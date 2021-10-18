from copy import deepcopy

# import plotly.express as px
import torch  # type: ignore
from torch import nn  # type: ignore
from pytorch_lightning import LightningModule  # type: ignore
import pandas as pd
import numpy as np
from sklearn.cluster import KMeans
from sklearn.metrics.cluster import adjusted_mutual_info_score
from sklearn.metrics import recall_score

from . import data_management, utils


def ema(values: list, min_periods: int, alpha: float):
    ema = pd.DataFrame(values).ewm(alpha=alpha,
                                   min_periods=min_periods).mean().values[-1,
                                                                          0]
    return ema


def get_conv(inchannels, outchannels, kernel, grouped, transposed, **kwargs):
    groups = 1
    if transposed:
        conv = nn.ConvTranspose2d
        if grouped:
            groups = outchannels
    else:
        conv = nn.Conv2d
        if grouped:
            groups = inchannels
    return conv(inchannels,
                outchannels,
                kernel,
                groups=groups,
                bias=False,
                **kwargs)


class ResidualBlock(nn.Module):
    def __init__(self,
                 inchannels,
                 outchannels,
                 activation,
                 reduce=False,
                 kernel=3,
                 transposed=False):
        super().__init__()
        self.inchannels = inchannels
        self.outchannels = outchannels
        self.activation = activation
        self.reduce = reduce
        self.transposed = transposed
        self.padding = 'valid' if reduce else 'same'
        self.stack = nn.Sequential(
            get_conv(inchannels,
                     outchannels,
                     kernel,
                     True,
                     transposed,
                     padding=self.padding),
            nn.BatchNorm2d(outchannels),
            activation,
            get_conv(outchannels, outchannels, 1, False, transposed),
            nn.BatchNorm2d(outchannels),
            activation,
        )

        self.kernel = (kernel, kernel)
        if not reduce:
            if inchannels == outchannels:
                self.proj = None
            else:
                self.proj = get_conv(inchannels, outchannels, 1, True,
                                     transposed)
        else:
            self.proj = get_conv(inchannels, outchannels, kernel, True,
                                 transposed)

    def forward(self, x):
        if not self.reduce:
            _x = self.stack(x)
            if not self.proj:
                out = _x + x
            else:
                out = _x + self.proj(x)
        else:
            out = self.stack(x) + self.proj(x)  # type: ignore
        self.out = out
        return out

    def outsize(self, insize):
        if not self.reduce:
            return insize
        elif self.transposed:
            return tuple(
                [insize[i] + self.kernel[i] - 1 for i in range(len(insize))])
        else:
            return tuple(
                [insize[i] - self.kernel[i] + 1 for i in range(len(insize))])


class ResidualStack(nn.Module):
    def __init__(self,
                 nblocks,
                 inchannels,
                 outchannels,
                 activation,
                 transposed,
                 kernel=3):
        super().__init__()
        self.nblocks = nblocks
        self.inchannels = inchannels
        self.outchannels = outchannels
        self.activation = activation
        self.transposed = transposed
        self.kernel = kernel
        stack = []
        for _ in range(nblocks - 1):
            stack.append(
                ResidualBlock(inchannels,
                              outchannels,
                              activation,
                              reduce=False,
                              transposed=transposed,
                              kernel=kernel))
            inchannels = outchannels

        stack.append(
            ResidualBlock(inchannels,
                          outchannels,
                          activation,
                          reduce=True,
                          transposed=transposed,
                          kernel=kernel))
        self.stack = nn.Sequential(*stack)

    def outsize(self, insize):
        outsize = insize
        for l in self.stack:
            outsize = l.outsize(outsize)
        return outsize

    def forward(self, x):
        self.out = self.stack(x)
        return self.out

    def get_outputs(self):
        out = []
        for block in self.stack[::-1]:
            out.append(block.out)
        return out


class Encoder(LightningModule):
    def __init__(self, insize, dropout, k1, k2, activation, kernel):

        super().__init__()
        self.dropout = nn.Dropout(dropout)
        self.insize = insize
        self.activation = activation
        self.kernel = kernel

        # make the stack
        stack = []
        outchannels = 1
        while insize[0] > kernel and insize[1] > kernel:
            inchannels = outchannels
            nblocks = max(1, int(2**k1 / outchannels))
            outchannels *= k2
            blocks = ResidualStack(nblocks,
                                   inchannels,
                                   outchannels,
                                   activation,
                                   transposed=False,
                                   kernel=kernel)
            stack.append(blocks)
            insize = blocks.outsize(insize)

        # add one convolution to reduce the size to 1x1
        stack.append(
            nn.Sequential(nn.Conv2d(outchannels, outchannels, insize),
                          nn.BatchNorm2d(outchannels), activation))

        self.stack = nn.Sequential(*stack)
        self.outchannels = outchannels

    def forward(self, x):

        x = self.dropout(x)

        # unsqueeze the channels dimension
        x = x.unsqueeze(1).expand(x.shape[0], 1, x.shape[1], x.shape[2])

        # apply the stack
        self.out = self.stack(x)

        # output has shape (batches, self.outchannels, 1, 1)
        return self.out

    def get_outputs(self):
        out = []
        # out.append(self.out)
        for layer in self.stack[::-1]:
            if type(layer) == ResidualStack:
                out += layer.get_outputs()
        return out


class Specializer(LightningModule):
    def __init__(self, hparams, loss_fn, nout):
        """
        A stack of linear layers that transform `features` into only one
        feature.

        Accept the output of the decoder, having shape: (batches,
        features, 1, 1).

        Returns a tensor with shape (batches, 1)

        * `hyperparams` must contains the following values:

            * middle_features: int [x in k*(2^x)]
            * num_layers: int
            * input_features: int [x in k*(2^x)]
            * middle_activation: callable
            * k: int
        """
        super().__init__()

        self.loss_fn = loss_fn
        middle_features, num_layers, input_features,\
            middle_activation, k = hparams

        middle_features = k * (2**middle_features)
        # input_features = k * (2**input_features)

        stack = []
        for _ in range(num_layers - 2):
            stack.append(nn.Linear(middle_features, middle_features))
            stack.append(
                nn.BatchNorm1d(middle_features,
                               affine=True,
                               track_running_stats=True))
            stack.append(middle_activation)
        self.stack = nn.Sequential(
            nn.Linear(input_features, middle_features),
            nn.BatchNorm1d(middle_features,
                           affine=True,
                           track_running_stats=True), middle_activation,
            *stack, nn.Linear(middle_features, nout),
            nn.Sigmoid() if nout == 1 else nn.Identity())

    def forward(self, x):
        return self.stack(x[:, :, 0, 0])

    def training_step(self, batch, batch_idx):

        out = self.forward(batch['x'])
        if out.shape[1] > 1:
            loss = self.loss_fn(out, batch['y'])
        else:
            loss = self.loss_fn(out, batch['y'].unsqueeze(-1))

        return {'loss': loss}

    def validation_step(self, batch, batch_idx):

        out = self.forward(batch['x'])
        if out.shape[1] > 1:
            loss = self.loss_fn(out, batch['y'])
            out = torch.argmax(out, 1)
        else:
            loss = self.loss_fn(out, batch['y'].unsqueeze(-1))

        return {'out': out, 'loss': loss}


class EncoderPerformer(LightningModule):
    """
    An iterative transfer-learning LightningModule for
    context-aware transcription
    """
    def __init__(self,
                 encoder,
                 performer,
                 cont_classifier,
                 contexts,
                 mode,
                 context_specific,
                 lr=1,
                 wd=0,
                 ema_period=20,
                 ema_alpha=0.5,
                 njobs=0,
                 perfm_testloss=nn.L1Loss(reduction='none')):
        super().__init__()
        self.encoder = encoder
        self.performers = nn.ModuleDict(
            {str(c): deepcopy(performer)
             for c in range(len(contexts))})
        self.context_specific = context_specific
        if self.context_specific:
            self.context_classifier = cont_classifier
        self.lr = lr
        self.wd = wd
        self.mode = mode
        self.contexts = contexts
        self.loss_pool = {"cont": [], "perfm": []}
        self.ema_loss_pool = {"cont": [], "perfm": []}
        self.ema_period = ema_period
        self.ema_alpha = ema_alpha
        self.njobs = njobs
        self.perfm_testloss = perfm_testloss
        self.test_latent_x = []
        self.test_latent_y = []

    def forward(self, x, context):
        enc_out = self.encoder.forward(x)
        perfm_out = self.performers[context].forward(enc_out)
        return perfm_out

    def training_step(self, batch, batch_idx):

        context_s = batch['c'][0]
        context_i = int(context_s)
        enc_out = self.encoder.forward(batch['x'])
        perfm_out = self.performers[context_s].training_step(
            {
                'x': enc_out,
                'y': batch['y']
            }, batch_idx)
        loss = perfm_out['loss']
        out = {'loss': loss, 'perfm_train_loss': perfm_out['loss'].detach()}
        if self.context_specific:
            cont_out = self.context_classifier.training_step(
                {
                    'x':
                    enc_out,
                    'y':
                    torch.tensor(context_i,
                                 device=enc_out.device,
                                 dtype=torch.long).expand(enc_out.shape[0])
                }, batch_idx)
            loss = loss + cont_out['loss']
            self.losslog('cont_train_loss', cont_out['loss'])
            out['cont_train_loss'] = cont_out['loss'].detach()

        lr_scheduler = self.lr_schedulers()
        if lr_scheduler is not None:
            lr_scheduler.step()

        self.losslog('train_loss', loss)
        self.losslog('perfm_train_loss', perfm_out['loss'])
        return out

    def validation_step(self, batch, batch_idx):

        context_s = batch['c'][0]
        context_i = int(context_s)
        enc_out = self.encoder.forward(batch['x'])
        perfm_out = self.performers[context_s].validation_step(
            {
                'x': enc_out,
                'y': batch['y']
            }, batch_idx)
        loss = perfm_out['loss']
        out = {'loss': loss, 'perfm_val_loss': perfm_out['loss'].detach()}
        if self.context_specific:
            cont_out = self.context_classifier.validation_step(
                {
                    'x':
                    enc_out,
                    'y':
                    torch.tensor(context_i,
                                 device=enc_out.device,
                                 dtype=torch.long).expand(enc_out.shape[0])
                }, batch_idx)
            loss = loss + cont_out['loss']
            out['cont_val_loss'] = cont_out['loss'].detach()
            self.loss_pool["cont"].append(
                cont_out["loss"].cpu().numpy().tolist())

        self.loss_pool["perfm"].append(
            perfm_out["loss"].cpu().numpy().tolist())
        self.losslog('val_loss', loss)
        self.losslog('perfm_val_loss', perfm_out['loss'])
        if self.context_specific:
            self.losslog('cont_val_loss', cont_out['loss'])
        return out

    def on_validation_epoch_end(self):
        # compute loss average and log ema
        self.ema_loss_pool["cont"].append(np.mean(self.loss_pool["cont"]))
        self.ema_loss_pool["perfm"].append(np.mean(
            self.loss_pool["perfm"]))
        cont_ema = ema(self.ema_loss_pool["cont"], self.ema_period,
                       self.ema_alpha)
        perfm_ema = ema(self.ema_loss_pool["perfm"], self.ema_period,
                        self.ema_alpha)
        self.losslog('cont_val_loss_avg', cont_ema)
        self.losslog('perfm_val_loss_avg', perfm_ema)
        for key, val in self.performer_weight_moments().items():
            self.losslog("weight_variance_" + key, val)

    def test_step(self, batch, batch_idx):

        context_s = batch['c'][0]
        context_i = int(context_s)
        enc_out = self.encoder.forward(batch['x'])
        batch_y = torch.tensor(context_i,
                               device=enc_out.device,
                               dtype=torch.long).expand(enc_out.shape[0])
        perfm_out = self.performers[context_s].validation_step(
            {
                'x': enc_out,
                'y': batch['y']
            }, batch_idx)['out']
        if self.context_specific:
            cont_out = self.context_classifier.validation_step(
                {
                    'x': enc_out,
                    'y': batch_y
                }, batch_idx)['out'].cpu().numpy()
        else:
            cont_out = None

        # record latents variables for clustering
        self.test_latent_x.append(enc_out.cpu().numpy())
        self.test_latent_y.append(batch_y.cpu().numpy())
        # * add test_epoch_end in which latent variables are clusterized
        return self.perfm_testloss(batch['y'],
                                   perfm_out[:, 0]).cpu().numpy(), cont_out

    def test_epoch_end(self, outputs, log=True):
        perfm_outputs = np.concatenate([o[0] for o in outputs])
        perfm_out_avg = np.mean(perfm_outputs)
        perfm_out_std = np.std(perfm_outputs)

        cont_labels = np.concatenate(self.test_latent_y)
        if self.context_specific:
            cont_outputs = np.concatenate([o[1] for o in outputs])
            cont_recalls = recall_score(cont_labels,
                                        cont_outputs,
                                        average=None)
            cont_bal_acc = np.mean(cont_recalls)
            if log:
                self.losslog('cont_bal_acc', cont_bal_acc)
                for i, rec in enumerate(cont_recalls):
                    self.losslog(f'rec_test_{i}', rec)

        cluster_computer = KMeans(n_clusters=len(self.contexts))
        labels = cluster_computer.fit_predict(
            np.concatenate(self.test_latent_x)[:, :, 0, 0])
        ami = adjusted_mutual_info_score(cont_labels, labels)

        if log:
            self.losslog('perfm_test_avg', perfm_out_avg)
            self.losslog('perfm_test_std', perfm_out_std)
            self.losslog('test_ami', ami)

    def performer_weight_moments(self):
        """
        Computes the average variance of the weights of the performers
        after having put the weights tensors in the same order as the first
        performer
        
        see https://math.stackexchange.com/questions/3225410/find-a-permutation-of-the-rows-of-a-matrix-that-minimizes-the-sum-of-squared-err
        """
        # get the number of tensor weights in the performers
        N = len(list(self.performers['0'].parameters()))
        permutations = [None] * len(self.performers)
        s = []
        for i in range(N):
            # get the list of the i-th layer parameters
            params = [
                list(perf.parameters())[i]
                for perf in self.performers.values()
            ]
            for j in range(len(self.performers)):
                if j > 0:
                    # retrieving last permutation
                    perm_cols = permutations[j]
                    if params[0].data.ndim > 1:
                        # a new linear layer
                        # computing and updating row permutation
                        perm_rows = utils.permute_tensors(params[0], params[j])
                        permutations[j] = perm_rows
                        if params[0].shape[0] > 1:
                            # apply row permutation
                            params[j] = params[j][perm_rows]
                        if perm_cols is not None:
                            # apply row permutation of the previous layer to the columns of this layer
                            params[j] = params[j][:, perm_cols]
                    elif perm_cols is not None:
                        # this is bias or batch normalization
                        # apply row permutation of the previous layer to this array
                        params[j] = params[j][perm_cols]
            # compute point-wise variances
            v = torch.var(torch.stack(params), dim=(0, ), unbiased=True)
            # append to the list the average variance
            s.append(torch.mean(v))
        return utils.torch_moments(torch.stack(s))

    def losslog(self, name, value):
        self.log(name,
                 value,
                 on_step=False,
                 on_epoch=True,
                 prog_bar=True,
                 logger=True)

    def configure_optimizers(self):
        return torch.optim.Adadelta(self.parameters(),
                                    lr=self.lr,
                                    weight_decay=0)

    def train_dataloader(self):
        dataloader = data_management.get_loader(['train'],
                                                False,
                                                self.contexts,
                                                self.mode,
                                                njobs=self.njobs)
        return dataloader

    def val_dataloader(self):
        dataloader = data_management.get_loader(['validation'],
                                                False,
                                                self.contexts,
                                                self.mode,
                                                njobs=self.njobs)
        return dataloader

    def test_dataloader(self):
        dataloader = data_management.get_loader(['test'],
                                                False,
                                                self.contexts,
                                                self.mode,
                                                njobs=1)
        # for some reason there are leakings with njobs > 1
        return dataloader
