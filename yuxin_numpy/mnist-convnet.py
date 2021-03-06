#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File: mnist-convnet.py

import os, sys

import argparse
import tensorflow as tf
import time

import numpy as np

"""
MNIST ConvNet example.
about 0.6% validation error after 30 epochs.
"""


# Just import everything into current namespace
from tensorpack import *
from tensorpack.tfutils import summary
from tensorpack.dataflow import dataset

IMAGE_SIZE = 28


class Model(ModelDesc):
    def _get_inputs(self):
        """
        Define all the inputs (with type, shape, name) that
        the graph will need.
        """
        return [InputDesc(tf.float32, (None, IMAGE_SIZE, IMAGE_SIZE), 'input'),
                InputDesc(tf.int32, (None,), 'label')]

    def _build_graph(self, inputs):
        """This function should build the model which takes the input variables
        and define self.cost at the end"""

        # inputs contains a list of input variables defined above
        image, label = inputs

        # In tensorflow, inputs to convolution function are assumed to be
        # NHWC. Add a single channel here.
        image = tf.expand_dims(image, 3)

        image = image * 2 - 1   # center the pixels values at zero

        # The context manager `argscope` sets the default option for all the layers under
        # this context. Here we use 32 channel convolution with shape 3x3
        with argscope(Conv2D, kernel_shape=3, nl=tf.nn.relu, out_channel=32):
            logits = (LinearWrap(image)
                      .Conv2D('conv0')
                      .MaxPooling('pool0', 2)
                      .Conv2D('conv1')
                      .Conv2D('conv2')
                      .MaxPooling('pool1', 2)
                      .Conv2D('conv3')
                      .FullyConnected('fc0', 512, nl=tf.nn.relu)
                      .Dropout('dropout', 0.5)
                      .FullyConnected('fc1', out_dim=10, nl=tf.identity)())

        tf.nn.softmax(logits, name='prob')   # a Bx10 with probabilities

        # a vector of length B with loss of each sample
        cost = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits, labels=label)
        cost = tf.reduce_mean(cost, name='cross_entropy_loss')  # the average cross-entropy loss

        correct = tf.cast(tf.nn.in_top_k(logits, label, 1), tf.float32, name='correct')
        accuracy = tf.reduce_mean(correct, name='accuracy')

        # This will monitor training error (in a moving_average fashion):
        # 1. write the value to tensosrboard
        # 2. write the value to stat.json
        # 3. print the value after each epoch
        train_error = tf.reduce_mean(1 - correct, name='train_error')
        summary.add_moving_summary(train_error, accuracy)

        # Use a regex to find parameters to apply weight decay.
        # Here we apply a weight decay on all W (weight matrix) of all fc layers
        wd_cost = tf.multiply(1e-5,
                              regularize_cost('fc.*/W', tf.nn.l2_loss),
                              name='regularize_loss')
        self.cost = tf.add_n([wd_cost, cost], name='total_cost')
        summary.add_moving_summary(cost, wd_cost, self.cost)

        # monitor histogram of all weight (of conv and fc layers) in tensorboard
        summary.add_param_summary(('.*/W', ['histogram', 'rms']))

    def _get_optimizer(self):
        lr = tf.train.exponential_decay(
            learning_rate=1e-3,
            global_step=get_global_step_var(),
            decay_steps=468 * 10,
            decay_rate=0.3, staircase=True, name='learning_rate')
        # This will also put the summary in tensorboard, stat.json and print in terminal
        # but this time without moving average
        tf.summary.scalar('lr', lr)
        return tf.train.AdamOptimizer(lr)


def get_data():
    train = BatchData(dataset.Mnist('train'), 10000)
    test = BatchData(dataset.Mnist('test'), 256, remainder=True)
    return train, test


def get_config():
    dataset_train, dataset_test = get_data()
    # How many iterations you want in each epoch.
    # This is the default value, don't actually need to set it in the config
    steps_per_epoch = dataset_train.size()

    # get the config which contains everything necessary in a training
    return TrainConfig(
        model=Model(),
        dataflow=dataset_train,  # the DataFlow instance for training
        callbacks=[
            GPUUtilizationTracker(),
            ModelSaver(),   # save the model after every epoch
            MaxSaver('validation_accuracy'),  # save the model with highest accuracy (prefix 'validation_')
            InferenceRunner(    # run inference(for validation) after every epoch
                dataset_test,   # the DataFlow instance used for validation
                ScalarStats(['cross_entropy_loss', 'accuracy'])),
        ],
        steps_per_epoch=steps_per_epoch,
        max_epoch=1000,
    )


step_count = 0

class NumpyTrainer(SyncMultiGPUTrainerReplicated):

    var_values = None

    def _setup_graph(self, input, get_cost_fn, get_opt_fn):
        callbacks = super(NumpyTrainer, self)._setup_graph(input, get_cost_fn, get_opt_fn)
        self.all_vars = []  # #GPU x #PARAM
        for grads in self._builder.grads:
            self.all_vars.append([k[1] for k in grads])
        self.all_grads = [k[0] for k in self._builder.grads[0]]

        self.acc_values = None
        return callbacks

    def _get_values(self):
        self.var_values = self.sess.run(self.all_vars[0])

    def _set_values(self):
        for all_vars in self.all_vars:
            for val, var in zip(self.var_values, all_vars):
                var.load(val)

    def run_step(self):
        global step_count
        step_count+=1
        if self.var_values is None:
            self._get_values()

        start_time = time.perf_counter()
        grad_values = self.hooked_sess.run(self.all_grads)
        duration = time.perf_counter() - start_time
        if step_count%10==0:
          self.monitors.put_scalar('step_time', duration)
        lr = 0.01
        momentum = 0.9

        if not self.acc_values:
          self.acc_values = [np.zeros_like(g) for g in grad_values]

        # from https://github.com/tensorflow/tensorflow/blob/982549ea3423df4270ff154e5c764beb43d472da/tensorflow/core/kernels/training_ops_gpu.cu.cc
        for i in range(len(self.var_values)):
          v = self.var_values[i]
          g = grad_values[i]
          self.acc_values[i] = self.acc_values[i]*momentum+g
          v -= lr*self.acc_values[i]
          
#        for v, g, acc in zip(self.var_values, grad_values, acc_values):
#            acc = acc*momentum + g
#            v -= lr * acc
#            import pdb; pdb.set_trace
#            #          v -= lr * g

        self._set_values()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', default='1', help='comma separated list of GPU(s) to use.')
    parser.add_argument('--load', help='load model')
    parser.add_argument('--name', default='mnist-convnet', help='load model')
    parser.add_argument('--group', default='yuxin_numpy', help='load model')
    args = parser.parse_args()
    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
        os.environ['CUDA_DEVICE_ORDER']='PCI_BUS_ID'


    # automatically setup the directory train_log/mnist-convnet for logging
    #logger.auto_set_dir()
    logger.set_logger_dir('/efs/runs/'+args.group+'/'+args.name)
    
    config = get_config()
    if args.load:
        config.session_init = SaverRestore(args.load)
    launch_train_with_config(config, NumpyTrainer(1, use_nccl=False))
