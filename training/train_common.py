import sys
import os
import math
sys.path.append("..")

import numpy as np

from model import get_training_model, get_lrmult
from training.optimizers import MultiSGD
from keras.callbacks import LearningRateScheduler, ModelCheckpoint, CSVLogger, TensorBoard, TerminateOnNaN
from keras.applications.vgg19 import VGG19
import keras.backend as K

from glob import glob
from config import GetConfig
import h5py

base_lr = 2e-5
momentum = 0.9
weight_decay = 5e-4
lr_policy = "step"
gamma = 0.333
stepsize = 121746 * 17  # in original code each epoch is 121746 and step change is on 17th epoch
max_iter = 200

def get_last_epoch_and_weights_file(WEIGHT_DIR, WEIGHTS_SAVE):

    os.makedirs(WEIGHT_DIR, exist_ok=True)
    files = [file for file in glob(WEIGHT_DIR + '/weights.*.h5')]
    files = [file.split('/')[-1] for file in files]
    epochs = [file.split('.')[1] for file in files if file]
    epochs = [int(epoch) for epoch in epochs if epoch.isdigit() ]
    if len(epochs) == 0:
        if 'weights.best.h5' in files:
            return -1, WEIGHT_DIR + '/weights.best.h5'
    else:
        ep = max([int(epoch) for epoch in epochs])
        return ep, WEIGHT_DIR + '/' + WEIGHTS_SAVE.format(epoch=ep)
    return None, None


# save names will be looking like
# training/canonical/exp1
# training/canonical_exp1.csv
# training/canonical/exp2
# training/canonical_exp2.csv

def prepare(config_name, exp_id, train_samples, val_samples, batch_size ):

    config = GetConfig(config_name)

    metrics_id = config_name + "_" + exp_id if exp_id is not None else config_name
    weights_id = config_name + "/" + exp_id if exp_id is not None else config_name

    WEIGHT_DIR = "./" + weights_id
    WEIGHTS_SAVE = 'weights.{epoch:04d}.h5'

    TRAINING_LOG = "./" + metrics_id + ".csv"
    LOGS_DIR = "./logs"

    model = get_training_model(weight_decay, np_branch1=config.paf_layers, np_branch2=config.heat_layers+1)
    lr_mult = get_lrmult(model)

    # load previous weights or vgg19 if this is the first run
    last_epoch, wfile = get_last_epoch_and_weights_file(WEIGHT_DIR, WEIGHTS_SAVE)

    if wfile is not None:
        print("Loading %s ..." % wfile)

        model.load_weights(wfile)

    else:
        print("Loading vgg19 weights...")

        vgg_model = VGG19(include_top=False, weights='imagenet')

        from_vgg = dict()
        from_vgg['conv1_1'] = 'block1_conv1'
        from_vgg['conv1_2'] = 'block1_conv2'
        from_vgg['conv2_1'] = 'block2_conv1'
        from_vgg['conv2_2'] = 'block2_conv2'
        from_vgg['conv3_1'] = 'block3_conv1'
        from_vgg['conv3_2'] = 'block3_conv2'
        from_vgg['conv3_3'] = 'block3_conv3'
        from_vgg['conv3_4'] = 'block3_conv4'
        from_vgg['conv4_1'] = 'block4_conv1'
        from_vgg['conv4_2'] = 'block4_conv2'

        for layer in model.layers:
            if layer.name in from_vgg:
                vgg_layer_name = from_vgg[layer.name]
                layer.set_weights(vgg_model.get_layer(vgg_layer_name).get_weights())
                print("Loaded VGG19 layer: " + vgg_layer_name)

        last_epoch = 0

    # euclidean loss as implemented in caffe https://github.com/BVLC/caffe/blob/master/src/caffe/layers/euclidean_loss_layer.cpp
    def eucl_loss(x, y):
        l = K.sum(K.square(x - y)) / batch_size / 2
        return l

    # learning rate schedule - equivalent of caffe lr_policy =  "step"
    iterations_per_epoch = train_samples // batch_size

    def step_decay(epoch):
        steps = epoch * iterations_per_epoch * batch_size
        lrate = base_lr * math.pow(gamma, math.floor(steps/stepsize))
        print("Epoch:", epoch, "Learning rate:", lrate)
        return lrate

    print("Weight decay policy...")
    for i in range(1,100,5): step_decay(i)

    # configure callbacks
    lrate = LearningRateScheduler(step_decay)
    checkpoint = ModelCheckpoint(WEIGHT_DIR + '/' + WEIGHTS_SAVE, monitor='loss', verbose=0, save_best_only=False, save_weights_only=True, mode='min', period=1)
    csv_logger = CSVLogger(TRAINING_LOG, append=True)
    tb = TensorBoard(log_dir=LOGS_DIR, histogram_freq=0, write_graph=True, write_images=False)
    tnan = TerminateOnNaN()
    #coco_eval = CocoEval(train_client, val_client)

    callbacks_list = [lrate, checkpoint, csv_logger, tb, tnan]

    # sgd optimizer with lr multipliers
    multisgd = MultiSGD(lr=base_lr, momentum=momentum, decay=0.0, nesterov=False, lr_mult=lr_mult)

    # start training

    model.compile(loss=eucl_loss, optimizer=multisgd)

    return model, iterations_per_epoch, val_samples//batch_size, last_epoch, metrics_id, callbacks_list




def train(model, train_di, val_di, iterations_per_epoch, validation_steps, last_epoch, use_client_gen, callbacks_list):

    model.fit_generator(train_di,
                        steps_per_epoch=iterations_per_epoch,
                        epochs=max_iter,
                        callbacks=callbacks_list,
                        validation_data=val_di,
                        validation_steps=validation_steps,
                        use_multiprocessing=not use_client_gen,  # TODO: zmq hangups somewhere in threads. fix it.
                        initial_epoch=last_epoch
                        )

def validate(model, val_di, validation_steps, use_client_gen):

        loss = model.evaluate_generator(val_di,
                                        steps=validation_steps,
                                        use_multiprocessing=not use_client_gen)
        print(loss)


def validate_batch(model, val_di, validation_steps, metrics_id):

    results = []

    for i in range(validation_steps):
        X, Y = next(val_di)

        loss = model.test_on_batch(X, Y)
        print(loss)
        results += [loss]

    np.savetxt(metrics_id + ".txt", np.array(results))


def save_network_input_output(model, val_di, validation_steps, metrics_id, batch_size):

    h5 = h5py.File(metrics_id + ".h5", 'w')

    for i in range(validation_steps):
        X, Y = next(val_di)

        grp = h5.create_group("%06d" % i)

        for n, v in enumerate(X):
            grp['x%02d' % n] = v

        for n, v in enumerate(Y):
            grp['gt%02d' % n] = v

        if model is not None:

            Yp = model.predict(X, batch_size=batch_size)

            for n, v in enumerate(Yp):
                grp['y%02d' % n] = v

        print(i)

    h5.close()
