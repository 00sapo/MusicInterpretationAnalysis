# from memory_profiler import profile

import os
from copy import deepcopy
from pathlib import Path
from pprint import pprint

import torch.nn.functional as F  # type: ignore
from torch import nn  # type: ignore
import torch  # type: ignore
import torchinfo
from pytorch_lightning import Trainer  # type: ignore
from pytorch_lightning.callbacks import ModelCheckpoint, StochasticWeightAveraging  # type: ignore
from pytorch_lightning.callbacks.early_stopping import EarlyStopping  # type: ignore
from pytorch_lightning.loggers import MLFlowLogger  # type: ignore

from . import data_management, feature_extraction
from . import settings as s
from .mytorchutils import best_checkpoint_saver, compute_average, count_params
from .asmd_resynth import get_contexts

RANDGEN = torch.Generator()

def model_test(model_build_func, test_sample):
    """
    A function to build a constraint around the model size; the constraint
    tries to build the model and to use it with a give ntest sample (can be
    created randomly, but shapes must be similar to a real case)
    """
    def constraint(hpar):
        print("----------")
        print("checking this set of hpar: ")
        pprint(hpar)
        allowed = True

        if allowed:
            model = None
            try:
                model = model_build_func(hpar)
                print("model created")
                model.eval()
                with torch.no_grad():
                    model.to(s.DEVICE).validation_step(
                        {
                            'x': test_sample.to(s.DEVICE).to(s.DTYPE),
                            'y': torch.tensor(0.5).to(s.DEVICE).to(s.DTYPE),
                            'ae_diff': test_sample.to(s.DEVICE).to(s.DTYPE),
                            'ae_same': test_sample.to(s.DEVICE).to(s.DTYPE),
                            'c': '0'
                        },
                        0,
                        log=False)
                print("model tested")
            # except Exception as e:
            #     import traceback
            #     traceback.print_exc(e)
            except Exception as e:
                print(e)
                allowed = False
            finally:
                del model

        print(f"hyper-parameters allowed: {allowed}")
        return allowed

    return constraint


def reconstruction_loss(pred, same_pred, diff_pred):
    return F.triplet_margin_with_distance_loss(
        pred,
        same_pred,
        diff_pred,
        distance_function=lambda x, y: 1 - F.cosine_similarity(x, y),
        margin=1,
        swap=True,
        reduction='sum')

def generic_loss(pred, same_pred, diff_pred):
    if torch.randint(6, (1,), generator=RANDGEN) > 0:
        return F.l1_loss(pred, diff_pred, reduction='sum')
    else:
        return F.l1_loss(pred, same_pred, reduction='sum')

def build_encoder(hpar, dropout, generic=False):
    if generic:
        loss_fn = generic_loss
    else:
        loss_fn = reconstruction_loss

    k1, k2, activation, kernel = get_hpar(hpar)

    m = feature_extraction.TripletEncoder(
        loss_fn=loss_fn,
        insize=(
            s.BINS,
            s.MINI_SPEC_SIZE),  # mini_spec_size should change for pedaling...
        dropout=dropout,
        k1=k1,
        k2=k2,
        activation=activation,
        kernel=2 * kernel + 1).to(s.DEVICE).to(s.DTYPE)
    # feature_extraction.init_weights(m, s.INIT_PARAMS)
    return m


def get_hpar(hpar):
    return (hpar['ae_k1'], hpar['ae_k2'], hpar['activation'], hpar['kernel'])


def build_performer_model(hpar, infeatures, avg_pred):
    m = feature_extraction.Performer(
        (hpar['performer_features'], hpar['performer_layers'], infeatures,
         hpar['activation'], 1), nn.L1Loss(reduction='sum'), avg_pred)

    return m


def build_model(hpar,
                mode,
                dropout=s.TRAIN_DROPOUT,
                dummy_avg=torch.tensor(0.5).cuda(),
                generic=True):

    contexts = list(get_contexts(s.CARLA_PROJ).keys())
    autoencoder = build_encoder(hpar, dropout, generic)
    performer = build_performer_model(hpar, autoencoder.encoder.outchannels,
                                      dummy_avg)
    model = feature_extraction.EncoderPerformer(autoencoder,
                                                performer,
                                                contexts,
                                                mode,
                                                ema_period=s.EMA_PERIOD)
    return model


def my_train(mode,
             copy_checkpoint,
             logger,
             model,
             generic,
             ae_train=True,
             perfm_train=True):
    """
    Creates callbacks, train and freeze the part that raised an early-stop.
    Return the best loss of that one and 0 for the other.
    """
    stopped_epoch = 9999  # let's enter the while the first time
    while stopped_epoch > s.EARLY_STOP:
        # setup callbacks
        checkpoint_saver = ModelCheckpoint(f"checkpoint_{mode}_generic={generic}",
                                           filename='{epoch}-{ae_loss:.2f}',
                                           monitor='val_loss',
                                           save_top_k=1,
                                           mode='min',
                                           save_weights_only=True)
        callbacks = [checkpoint_saver]
        ae_stopper = perfm_stopper = None
        if ae_train:
            ae_stopper = EarlyStopping(monitor='ae_val_loss_avg',
                                       min_delta=s.EARLY_RANGE,
                                       check_finite=False,
                                       patience=s.EARLY_STOP)
            callbacks.append(ae_stopper)
        if perfm_train:
            perfm_stopper = EarlyStopping(monitor='perfm_val_loss_avg',
                                          min_delta=s.EARLY_RANGE,
                                          check_finite=False,
                                          patience=s.EARLY_STOP)
            callbacks.append(perfm_stopper)
        if copy_checkpoint:
            callbacks.append(best_checkpoint_saver(copy_checkpoint))

        if s.SWA:
            callbacks.append(
                StochasticWeightAveraging(swa_epoch_start=int(0.8 * s.EPOCHS),
                                          annealing_epochs=int(0.2 * s.EPOCHS)))

    # training!
        trainer = Trainer(
            callbacks=callbacks,
            precision=s.PRECISION,
            max_epochs=s.EPOCHS,
            logger=logger,
            auto_lr_find=True,
            reload_dataloaders_every_n_epochs=1,
            # weights_summary="full",
            # log_every_n_steps=1,
            # log_gpu_memory=True,
            # track_grad_norm=2,
            # overfit_batches=1,
            gpus=s.GPUS)

        model.njobs = 1  # there's some leak when using njobs > 0
        if os.path.exists("lr_find_temp_model.ckpt"):
            os.remove("lr_find_temp_model.ckpt")
        d = trainer.tune(model, lr_find_kwargs=dict(min_lr=1e-7, max_lr=10))
        if d['lr_find'].suggestion() is None:
            model.lr = 1e-5
            model.learning_rate = 1e-5
        model.njobs = s.NJOBS
        print("Fitting the model!")
        trainer.fit(model)
        if ae_train:
            stopped_epoch = ae_stopper.stopped_epoch  # type: ignore
        else:
            stopped_epoch = 0
        if perfm_train:
            stopped_epoch = max(stopped_epoch,
                                perfm_stopper.stopped_epoch)  # type: ignore

    return ae_stopper, perfm_stopper


def train(hpar, mode, copy_checkpoint='', generic=False):
    """
    1. Builds a model given `hpar` and `mode`
    2. Train the model
    3. Saves the trained model weights to `copy_checkpoint`
    4. Returns the best validation loss function + the complexity penalty set
       in `settings`
    """
    # the logger
    logger = MLFlowLogger(experiment_name=f'{mode}',
                          tracking_uri=os.environ.get('MLFLOW_TRACKING_URI'))

    # dummy model (baseline)
    # TODO: check the following function!
    # dummy_avg = compute_average(trainloader.dataset,
    #                             *axes,
    #                             n_jobs=-1,
    #                             backend='threading').to(s.DEVICE)

    model = build_model(hpar, mode, generic=generic)
    torchinfo.summary(model)

    # training
    # this loss is also the same used for hyper-parameters tuning
    ae_stopper, perfm_stopper = my_train(mode, copy_checkpoint, logger, model, generic)
    ae_loss, perfm_loss = ae_stopper.best_score, perfm_stopper.best_score  # type: ignore
    print(
        f"First-training losses: {ae_loss:.2e}, {perfm_loss:.2e}"
    )

    # cases:
    # A: encoder was stopped
    # B: performers were stopped
    # C: none was stopped

    if ae_stopper.stopped_epoch == 0 and perfm_stopper.stopped_epoch > 0:  # type: ignore
        # case A
        model.performers.freeze()
        print("Continuing training encoder...")
        ae_stopper, _ = my_train(mode, copy_checkpoint, logger, model, generic,
                                 True, False)
    if ae_stopper.stopped_epoch > 0:  # type: ignore
        # case A and B
        model.tripletencoder.freeze()
        print("Continuing training performers...")
        _, perfm_stopper = my_train(mode, copy_checkpoint, logger, model,
                                    generic, False, True)

    ae_loss, perfm_loss = ae_stopper.best_score, perfm_stopper.best_score  # type: ignore
    print(f"Final losses: {ae_loss:.2e}, {perfm_loss:.2e}")

    if s.COMPLEXITY_PENALIZER > 0:
        complexity_loss = count_params(model) * s.COMPLEXITY_PENALIZER
    else:
        complexity_loss = 1.0
    loss = (ae_loss + perfm_loss) * complexity_loss  # type: ignore
    logger.log_metrics({
        "best_ae_loss": float(ae_loss),  # type: ignore
        "best_perfm_loss": float(perfm_loss),  # type: ignore
        "total_loss": float(loss),
        "final_weight_variance_norm": model.performer_weight_variance_norm()
    })

    # this is the loss used by hyper-parameters optimization
    return float(loss)
