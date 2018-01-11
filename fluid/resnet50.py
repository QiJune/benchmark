from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import functools
import numpy as np
import time

import cProfile, pstats, StringIO

import paddle.v2 as paddle
import paddle.v2.fluid as fluid
import paddle.v2.fluid.core as core
import paddle.v2.fluid.profiler as profiler
import paddle.v2.fluid.core as core


def parse_args():
    parser = argparse.ArgumentParser('Convolution model benchmark.')
    parser.add_argument(
        '--model',
        type=str,
        choices=['resnet'],
        default='resnet',
        help='The model architecture.')
    parser.add_argument(
        '--batch_size', type=int, default=32, help='The minibatch size.')
    parser.add_argument(
        '--use_fake_data',
        action='store_true',
        help='use real data or fake data')
    parser.add_argument(
        '--skip_batch_num',
        type=int,
        default=20,
        help='The first num of minibatch num to skip, for better performance test'
    )
    parser.add_argument(
        '--iterations',
        type=int,
        default=120,
        help='The number of final iteration.')
    parser.add_argument(
        '--pass_num', type=int, default=100, help='The number of passes.')
    parser.add_argument(
        '--step', type=int, default=100, help='The number of iterations showing a loss.')
    parser.add_argument(
        '--order',
        type=str,
        default='NCHW',
        choices=['NCHW', 'NHWC'],
        help='The data order, now only support NCHW.')
    parser.add_argument(
        '--device',
        type=str,
        default='CPU',
        choices=['CPU', 'GPU'],
        help='The device type.')
    parser.add_argument(
        '--infer_only', action='store_true', help='If set, run forward only.')
    parser.add_argument(
        '--use_cprof', action='store_true', help='If set, use cProfile.')
    parser.add_argument(
        '--use_nvprof',
        action='store_true',
        help='If set, use nvprof for CUDA.')
    args = parser.parse_args()
    return args


def print_arguments(args):
    vars(args)['use_nvprof'] = (vars(args)['use_nvprof'] and
                                vars(args)['device'] == 'GPU')
    print('-----------  Configuration Arguments -----------')
    for arg, value in sorted(vars(args).iteritems()):
        print('%s: %s' % (arg, value))
    print('------------------------------------------------')


def resnet(input, class_dim, depth=50, order='NCHW'):
    def conv_bn_layer(input, ch_out, filter_size, stride, padding, act='relu'):
        tmp = fluid.layers.conv2d(
            input=input,
            filter_size=filter_size,
            num_filters=ch_out,
            stride=stride,
            padding=padding,
            act=None,
            bias_attr=False)
        return fluid.layers.batch_norm(input=tmp, act=act)

    def shortcut(input, ch_out, stride):
        ch_in = input.shape[1] if order == 'NCHW' else input.shape[-1]
        if ch_in != ch_out:
            return conv_bn_layer(input, ch_out, 1, stride, 0, None)
        else:
            return input

    def basicblock(input, ch_out, stride):
        short = shortcut(input, ch_out, stride)
        conv1 = conv_bn_layer(input, ch_out, 3, stride, 1)
        conv2 = conv_bn_layer(conv1, ch_out, 3, 1, 1, act=None)
        return fluid.layers.elementwise_add(x=short, y=conv2, act='relu')

    def bottleneck(input, ch_out, stride):
        short = shortcut(input, ch_out * 4, stride)
        conv1 = conv_bn_layer(input, ch_out, 1, stride, 0)
        conv2 = conv_bn_layer(conv1, ch_out, 3, 1, 1)
        conv3 = conv_bn_layer(conv2, ch_out * 4, 1, 1, 0, act=None)
        return fluid.layers.elementwise_add(x=short, y=conv3, act='relu')

    def layer_warp(block_func, input, ch_out, count, stride):
        res_out = block_func(input, ch_out, stride)
        for i in range(1, count):
            res_out = block_func(res_out, ch_out, 1)
        return res_out

    cfg = {
        18: ([2, 2, 2, 1], basicblock),
        34: ([3, 4, 6, 3], basicblock),
        50: ([3, 4, 6, 3], bottleneck),
        101: ([3, 4, 23, 3], bottleneck),
        152: ([3, 8, 36, 3], bottleneck)
    }
    stages, block_func = cfg[depth]
    conv1 = conv_bn_layer(input, ch_out=64, filter_size=7, stride=2, padding=3)
    pool1 = fluid.layers.pool2d(
        input=conv1, pool_type='avg', pool_size=3, pool_stride=2)
    res1 = layer_warp(block_func, pool1, 64, stages[0], 1)
    res2 = layer_warp(block_func, res1, 128, stages[1], 2)
    res3 = layer_warp(block_func, res2, 256, stages[2], 2)
    res4 = layer_warp(block_func, res3, 512, stages[3], 2)
    pool2 = fluid.layers.pool2d(
        input=res4,
        pool_size=7,
        pool_type='avg',
        pool_stride=1,
        global_pooling=True)
    out = fluid.layers.fc(input=pool2, size=class_dim, act='softmax')
    return out


def run_benchmark(model, args):
    if args.use_cprof:
        pr = cProfile.Profile()
        pr.enable()
    start_time = time.time()

    class_dim = 102
    dshape = [3, 224, 224] if args.order == 'NCHW' else [224, 224, 3]
    input = fluid.layers.data(name='data', shape=dshape, dtype='float32')
    label = fluid.layers.data(name='label', shape=[1], dtype='int64')
    predict = model(input, class_dim)
    cost = fluid.layers.cross_entropy(input=predict, label=label)
    avg_cost = fluid.layers.mean(x=cost)
    optimizer = fluid.optimizer.Momentum(learning_rate=0.01, momentum=0.9)
    opts = optimizer.minimize(avg_cost)
    accuracy = fluid.evaluator.Accuracy(input=predict, label=label)

    train_reader = paddle.batch(
        paddle.reader.shuffle(
            paddle.dataset.flowers.train(), buf_size=5120),
        batch_size=args.batch_size)

    place = core.CPUPlace() if args.device == 'CPU' else core.CUDAPlace(0)
    exe = fluid.Executor(place)
    exe.run(fluid.default_startup_program())

    if args.use_fake_data:
        data = train_reader().next()
        image = np.array(map(lambda x: x[0].reshape(dshape), data)).astype(
            'float32')
        label = np.array(map(lambda x: x[1], data)).astype('int64')
        label = label.reshape([-1, 1])

    iter = 0
    im_num = 0
    for pass_id in range(args.pass_num):
        accuracy.reset(exe)
        if iter == args.iterations:
            break
        pass_start_time = time.time()
        batch_start_time = time.time()
        for batch_id, data in enumerate(train_reader()):
            if not args.use_fake_data:
                image = np.array(map(lambda x: x[0].reshape(dshape),
                                     data)).astype('float32')
                label = np.array(map(lambda x: x[1], data)).astype('int64')
                label = label.reshape([-1, 1])
            outs = exe.run(fluid.default_main_program(),
                           feed={'data': image,
                                 'label': label},
                           fetch_list=[avg_cost] + accuracy.metrics
                           if batch_id % args.step == 0 else [])

            if batch_id % args.step == 0:
                batch_end_time = time.time()
                pass_acc = accuracy.eval(exe)
                print(
                    "Pass_id:%d, batch_id:%d, Iter: %d, loss: %.5f, acc: %.5f, pass_acc: %.5f, elapse: %f"
                    % (pass_id, batch_id, iter, outs[0][0], outs[1][0],
                       pass_acc[0], (batch_end_time - batch_start_time)))
                batch_start_time = time.time()

            im_num += label.shape[0]
            if iter == args.skip_batch_num:
                start_time = time.time()
            if iter == args.iterations:
                break
            iter += 1

        pass_end_time = time.time()
        print("Iter: %d, elapse: %f" % (iter,
                                        (pass_end_time - pass_start_time)))
    duration = time.time() - start_time
    im_num = im_num - args.skip_batch_num * args.batch_size
    examples_per_sec = im_num / duration
    sec_per_batch = duration / (iter - args.skip_batch_num)

    print('\nTotal examples: %d, total time: %.5f' % (im_num, duration))
    print('%.5f examples/sec, %.5f sec/batch \n' %
          (examples_per_sec, sec_per_batch))

    if args.use_cprof:
        pr.disable()
        s = StringIO.StringIO()
        sortby = 'cumulative'
        ps = pstats.Stats(pr, stream=s).sort_stats(sortby)
        ps.print_stats()
        print(s.getvalue())


if __name__ == '__main__':
    model_map = {'resnet': resnet, }
    args = parse_args()
    print_arguments(args)
    if args.order == 'NHWC':
        raise ValueError('Only support NCHW order now.')
    if args.use_nvprof and args.device == 'GPU':
        with profiler.cuda_profiler("cuda_profiler.txt", 'csv') as nvprof:
            run_benchmark(model_map[args.model], args)
    else:
        run_benchmark(model_map[args.model], args)
