#!/usr/bin/env python
# -*- coding:utf-8 -*-  

from caffe2.proto import caffe2_pb2
from caffe2.python.predictor import mobile_exporter
from caffe2.python import (
    workspace,
    core,
    model_helper,
    brew,
    optimizer,
    utils,
    )
from data_utility import (
    data_augmentation,
    prepare_data,
    color_preprocessing,
    next_batch,
    next_batch_random,
    dummy_input,
    )
from models import create_lenet, create_resnet
import time
import tabulate
import numpy as np

USE_GPU = True
USE_AUGMENTATION = False
GPU_ID = 0
BATCH_SIZE = 128
EPOCHS = 200
EVAL_FREQ = 1
TRAIN_IMAGES = 50000
DEPTH = 5 # layers = depth * 6 + 2
TEST_IMAGES = 10000
INIT_NET = './init_net.pb'
PREDICT_NET = './predict_net.pb'


def add_sortmax(model, last_out, device_opts):
    with core.DeviceScope(device_opts):
        softmax = brew.softmax(model, last_out, 'softmax')
        return softmax

def add_softmax_with_loss(model, last_out, device_opts):
    with core.DeviceScope(device_opts):
        softmax, loss = model.net.SoftmaxWithLoss([last_out, "label"], ["softmax", "loss"])
        return softmax, loss

def add_accuracy(model, softmax, device_opts):
    with core.DeviceScope(device_opts):
        accuracy = brew.accuracy(model, [softmax, "label"], "accuracy")
        return accuracy

def add_training_operators(model, last_out, device_opts) :

    with core.DeviceScope(device_opts):

        softmax, loss = add_softmax_with_loss(model, last_out, device_opts)
        accuracy = add_accuracy(model, softmax, device_opts)

        model.AddGradientOperators([loss])
        opt = optimizer.build_sgd(
            model, 
            base_learning_rate=0.1, 
            policy="step", 
            stepsize=50000 * 80 // BATCH_SIZE, 
            weight_decay=1e-4,
            momentum=0.9, 
            gamma=0.1,
            nesterov=1,
            ) 
        # [Optional] feel free to use adam or other optimizers
        # opt = optimizer.build_adam(
        #     model, 
        #     base_learning_rate=1e-3,
        #     weight_decay=1e-4,
        #     )


def save_net(init_net_pb, predict_net_pb, model):
    extra_params = []
    extra_blobs = []
    for blob in workspace.Blobs():
        name = str(blob)
        if name.endswith("_rm") or name.endswith("_riv"):
            extra_params.append(name)
            extra_blobs.append(workspace.FetchBlob(name))
    for name, blob in zip(extra_params, extra_blobs):
        workspace.FeedBlob(name, blob)
        model.params.append(name)

    init_net, predict_net = mobile_exporter.Export(
        workspace, 
        model.net, 
        model.params
        )
    
    with open(predict_net_pb, 'wb') as f:
        f.write(model.net._net.SerializeToString())
    with open(init_net_pb, 'wb') as f:
        f.write(init_net.SerializeToString())

def load_net(init_net_pb, predict_net_pb, device_opts):
    init_def = caffe2_pb2.NetDef()
    with open(init_net_pb, 'rb') as f:
        init_def.ParseFromString(f.read())
        init_def.device_option.CopyFrom(device_opts)
        workspace.RunNetOnce(init_def.SerializeToString())
    
    net_def = caffe2_pb2.NetDef()
    with open(predict_net_pb, 'rb') as f:
        net_def.ParseFromString(f.read())
        net_def.device_option.CopyFrom(device_opts)
        workspace.CreateNet(net_def.SerializeToString(), overwrite=True)

def train_epoch(model, train_x, train_y):
    loss_sum = 0.0
    correct = 0.0
    batch_num = TRAIN_IMAGES // BATCH_SIZE + 1
    for i in range(0, batch_num):
        # data, label = next_batch(i, BATCH_SIZE, train_x, train_y, TRAIN_IMAGES)
        data, label = next_batch_random(BATCH_SIZE, train_x, train_y)
        if USE_AUGMENTATION:
            data = data_augmentation(data)

        workspace.FeedBlob("data", data, device_option=device_opts)
        workspace.FeedBlob("label", label, device_option=device_opts)
        workspace.RunNet(model.net) 
        loss_sum += workspace.FetchBlob("loss")
        correct += workspace.FetchBlob("accuracy")

    return {
        'loss': loss_sum / batch_num,
        'accuracy': correct / batch_num * 100.0,
        }

def eval(model, test_x, test_y):
    loss_sum = 0.0
    correct = 0.0
    batch_num = TEST_IMAGES // 1000

    for i in range(0, batch_num):
        # data, label = next_batch(i, 1000, test_x, test_y, TEST_IMAGES)
        data, label = next_batch_random(1000, test_x, test_y)
        
        workspace.FeedBlob("data", data, device_option=device_opts)
        workspace.FeedBlob("label", label, device_option=device_opts)

        workspace.RunNet(model.net) 
        loss_sum += workspace.FetchBlob("loss")
        correct += workspace.FetchBlob("accuracy")

    return {
        'loss': loss_sum / batch_num,
        'accuracy': correct / batch_num * 100.0,
        }

def do_train(init_net_pb, predict_net_pb, epochs, device_opts) :

    workspace.ResetWorkspace()

    data, label = dummy_input()
    workspace.FeedBlob("data", data, device_option=device_opts)
    workspace.FeedBlob("label", label, device_option=device_opts)

    train_arg_scope = {
        'order': 'NCHW',
        'use_cudnn': True,
    }

    train_model= model_helper.ModelHelper(name="train_net", arg_scope=train_arg_scope)
    last_out = create_resnet(
        model=train_model,
        data='data', 
        num_input_channels=3,
        num_groups=DEPTH,
        num_labels=10, 
        device_opts=device_opts,
        is_test=False)
    add_training_operators(train_model, last_out, device_opts=device_opts)

    test_model= model_helper.ModelHelper(name="test_net", init_params=False)
    last_out = create_resnet(
        model=test_model,
        data='data', 
        num_input_channels=3,
        num_groups=DEPTH,
        num_labels=10, 
        device_opts=device_opts,
        is_test=True)
    softmax, loss = add_softmax_with_loss(test_model, last_out, device_opts)
    add_accuracy(test_model, softmax, device_opts)

    workspace.RunNetOnce(train_model.param_init_net)
    workspace.CreateNet(train_model.net)

    workspace.RunNetOnce(test_model.param_init_net)
    workspace.CreateNet(test_model.net, overwrite=True)
    
    # print(workspace.Blobs())

    print('\ntraining for', epochs, 'epochs')
    columns = ['ep', 'lr', 'tr_loss', 'tr_acc', 'te_loss', 'te_acc', 'time']

    for e in range(0, epochs):
        time_ep = time.time()
        
        train_res = train_epoch(train_model, train_x, train_y)

        if e == 0 or e % EVAL_FREQ == 0 or e == epochs - 1:
            test_res = eval(test_model, test_x, test_y)
        else:
            test_res = {'loss': None, 'accuracy': None}
        
        time_ep = time.time() - time_ep

        lr = workspace.FetchBlob("SgdOptimizer_0_lr_gpu0")
        # lr = workspace.FetchBlob("AdamOptimizer_0_lr_gpu0")

        values = [
            e + 1, 
            lr, 
            train_res['loss'], 
            train_res['accuracy'], 
            test_res['loss'], 
            test_res['accuracy'], 
            time_ep,
            ]
        table = tabulate.tabulate([values], columns, tablefmt='simple', floatfmt='8.4f')
        if e % 40 == 0:
            table = table.split('\n')
            table = '\n'.join([table[1]] + table)
        else:
            table = table.split('\n')[2]
        print(table)
    print('training done')

    # save net to forward !!
    deploy_model= model_helper.ModelHelper(name="deploy_net", init_params=False)
    last_out = create_resnet(
        model=deploy_model,
        data='data', 
        num_input_channels=3,
        num_groups=DEPTH,
        num_labels=10, 
        device_opts=device_opts,
        is_test=True)
    add_sortmax(deploy_model, last_out, device_opts)

    workspace.RunNetOnce(deploy_model.param_init_net)
    workspace.CreateNet(deploy_model.net, overwrite=True)

    save_net(init_net_pb, predict_net_pb, deploy_model)

def do_test():
    print ('\n== loading deploy model to test ==')
    workspace.ResetWorkspace()
    load_net(INIT_NET, PREDICT_NET, device_opts=device_opts)
     
    test_batch = 10
    data, label = next_batch_random(test_batch, test_x, test_y)
    workspace.FeedBlob("data", data, device_option=device_opts)
    workspace.RunNet('deploy_net')

    print ('== done. ==')

    print ("\nInput: ones")
    print ("Output last_out:\n", workspace.FetchBlob("last_out"))
    print ("Output softmax:\n", workspace.FetchBlob("softmax"))
    print ("Output class: ", np.argmax(workspace.FetchBlob("softmax"),axis=1))
    print ("Real class  : ", label)

if __name__ == '__main__':
    
    # 1. Set global init level & Device Option: CUDA or CPU
    core.GlobalInit(['caffe2', '--caffe2_log_level=0'])
    if USE_GPU:
        device_opts = core.DeviceOption(caffe2_pb2.CUDA, GPU_ID)  
    else:
        device_opts = core.DeviceOption(caffe2_pb2.CPU, 0)

    # 2. Prepare data
    # try to download & extract
    # then do shuffle & -std/mean normalization
    train_x, train_y, test_x, test_y = prepare_data()
    train_x, test_x = color_preprocessing(train_x, test_x)

    # 3. Start training & save pb files.
    do_train(
        INIT_NET,
        PREDICT_NET,
        epochs=EPOCHS,
        device_opts=device_opts,
        )

    # 4. Do a test if you need
    do_test()

