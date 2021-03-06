import numpy as np
import json
import time
import os
import h5py
import tensorflow as tf

# import sys
# sys.path.insert(0, '../utils')

# from utils import define_gpu

# VFILE = './Data.h5'
# TFILE = './Validate.h5'


def Topk(array, k):
    top = np.argpartition(-array, k)[:k]
    pair = sorted(zip(array[top], top), key=lambda x: x[0], reverse=True)
    return [_[1] for _ in pair]


def data_iter(batch_size, data, d_len, label, k=None, shuffle_data=True):
    N = data.shape[0]

    steps = np.ceil(N / float(batch_size))
    steps = int(steps)
    yield steps

    if k :
        d_topk = np.zeros( [batch_size, k, 2], dtype=np.float32 )
    oh_label = np.zeros([batch_size, 3], dtype=np.int)

    order = range(batch_size)
    for s in range(steps):

        head = s * batch_size
        end = (s + 1) * batch_size
        if end <= N:
            d = data[head:end]
            dl = d_len[head:end]
            l = label[head:end]
        else:
            d = np.concatenate([data[head:], data[:end - N]], axis=0)
            l = np.concatenate([label[head:], label[:end - N]], axis=0)
            dl = np.concatenate([d_len[head:], d_len[:end - N]], axis=0)

        if shuffle_data:
            np.random.shuffle(order)
            d = d[order]
            dl = dl[order]
            l = l[order]


        oh_label.fill(0)

        try:
            oh_label[range(batch_size), l - 1] = 1
        except Exception, e:
            print e
            assert False 
            #import ipdb
            #ipdb.set_trace()


        if k:
            d_topk.fill(0)
            for i in range(batch_size):
                top_idx = np.argpartition(-d[i,:,1], k)[:k]
                top_idx = sorted(top_idx)
                d_topk[i] = d[i,top_idx,:] # i, 2, k

            dl[:] = k

            yield s, d_topk, dl, oh_label
        else:
            yield s, d,      dl, oh_label


def prepare_data(batch_size, fname, vocab_size=50003, topk=None, shuffle=True):

    with h5py.File(fname, 'r') as hf:
        data = hf.get('data')[()]
        dlen = hf.get('dlen')[()]
        label = hf.get('label')[()]

    itr = data_iter(batch_size, data, dlen, label, shuffle_data=shuffle, k=topk)
    step = itr.next()
    return itr, step


def create_flag():
    flags = tf.app.flags
    flags.DEFINE_integer("epoch", 15, "Epoch to train [40]")
    flags.DEFINE_integer("batch_size", 64, "")
    flags.DEFINE_integer("gpu", 1, "the number of gpus to use")
    flags.DEFINE_integer("data_size", None, "Number of files to train on")
    flags.DEFINE_integer("hidden_size", 64, "")
    flags.DEFINE_integer("eval_every", 400, "Eval every step")
    flags.DEFINE_integer("layer", 1, "")
    flags.DEFINE_integer("topk", None, "")

    flags.DEFINE_float("learning_rate", 5e-2, "Learning rate")
    flags.DEFINE_string("log_dir", "log", "")
    flags.DEFINE_string("load_path", None, "The path to old model. [None]")
    flags.DEFINE_string("data_path", './data/Data_Big.h5', "")
    flags.DEFINE_string("model", "onehot", "")

    # flags.DEFINE_string("optim", 'RMS', "The optimizer to use [RMS]")
    flags.DEFINE_boolean('reuse', True, '')

    FLAGS = flags.FLAGS

    # print FLAGS
    for k in FLAGS.__flags:
        print k, FLAGS.__flags[k]

    return FLAGS


def main():
    FLAGS = create_flag()

    # gpu_list = define_gpu( FLAGS.gpu )
    gpu_list = [ FLAGS.gpu ]
    os.environ['CUDA_VISIBLE_DEVICES'] = ",".join( map(str, gpu_list) )
    print 'Using GPU: %s' % gpu_list

    if FLAGS.model == 'origin':
        from model import RNN as m
    elif FLAGS.model == 'onehot':
        from model import One_Hot as m
    elif FLAGS.model == 'cnn':
        from model import CNN as m
    elif FLAGS.model == 'sigmoid':
        from model import Sigmoid as m
    else:
        raise ValueError(FLAGS.model)

    M = m(
            FLAGS.batch_size, FLAGS.hidden_size,
            learning_rate=FLAGS.learning_rate,
            sequence_length=FLAGS.topk if FLAGS.topk else 1000,
            num_layer=FLAGS.layer,
            reuse=FLAGS.reuse,
        )
    print 'Model Created'        

    log_dir = "%s/%s" % (FLAGS.log_dir, time.strftime("%m_%d_%H_%M"))
    save_dir = os.path.join(log_dir, 'ckpts')
    if os.path.exists(log_dir):
        print('log_dir exist %s' % log_dir)
        exit(2)
    os.makedirs(save_dir)
    with open(log_dir + '/Flags.js', 'w') as f:
        json.dump(FLAGS.__flags, f, indent=4)
    print 'Writing log to %s' % log_dir

    if 'New' in FLAGS.data_path:
        VFILE = './data/New_Val.h5'
    else: 
        VFILE = './data/Validate.h5'

    with tf.Session() as sess:
        writer = tf.train.SummaryWriter(log_dir, sess.graph)
        tfetch = [M.global_step, M.loss, M.accuracy, M.train_op,
                 M.train_summary,
                 M.prediction,
                 # M.prediction, M.right_label,
                 # M.correct
                 ]
        vfetch = [M.loss, M.accuracy, M.validate_summary]

        sess.run(tf.initialize_all_variables())

        running_acc = 0.0
        running_loss = 0.0
        for e in range(FLAGS.epoch):
            titer, tstep = prepare_data(
                FLAGS.batch_size, FLAGS.data_path, shuffle=True, topk=FLAGS.topk)

            print tstep

            for data in titer:
                gstep, loss, accuracy, _, sum_str, score = M.step(sess, data, tfetch)

                running_acc += accuracy
                running_loss += loss.mean()
                writer.add_summary(sum_str, gstep)

                if gstep % 20 == 0:
                    print '%d E[%d] Acc: %.4f Loss: %.4f' % \
                        (gstep, e, running_acc / 20.0, running_loss / 20.0)
                    running_acc = 0.0
                    running_loss = 0.0
                    
                if (gstep-1) % 5 == 0:
                    print 'prediction'
                    print score.mean()
                    print '\n\n'

                if gstep % FLAGS.eval_every == 0:

                    viter, vstep = prepare_data(FLAGS.batch_size, VFILE, shuffle=False, topk=FLAGS.topk)
                    vrunning_acc = 0.0
                    vrunning_loss = 0.0

                    for data in viter:
                        loss, accuracy, sum_str = M.step(sess, data, vfetch)
                        vrunning_acc += accuracy
                        vrunning_loss+= loss.mean()
                        writer.add_summary(sum_str, gstep+data[0])

                    print 'Evaluate Acc: %.4f Loss: %.4f' % \
                                    (vrunning_acc/vstep, vrunning_loss/vstep)



if __name__ == '__main__':
    main()