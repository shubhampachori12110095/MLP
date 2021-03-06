#! /usr/bin/python
import os
import sys
import tensorflow as tf
import time
import json
import numpy as np
from utils import pp
from mdu import batchIter
from mdu import restruct_glove_embedding
from mdu import prepare_data
from tensorflow.contrib.layers import l2_regularizer
from base import orthogonal_initializer
# from eval_tool import norm
from utils import define_gpu

flags = tf.app.flags

flags.DEFINE_integer("gpu", 1, "the number of gpus to use")
flags.DEFINE_integer("data_size", None, "Number of files to train on")
flags.DEFINE_float("eval_every", 100.0, "Eval every step")
flags.DEFINE_float("save_every", 500.0, "Eval every step")
flags.DEFINE_string("log_dir", "log", "Directory name to save the log [log]")
flags.DEFINE_string("dataset", "squad", "Data")
flags.DEFINE_string("load_path", None, "The path to old model.")
flags.DEFINE_string("weight", 'one', 'one, idf, tfidf')

flags.DEFINE_integer("epoch", 60, "Epoch to train")
flags.DEFINE_integer("vocab_size", 0, "The size of vocabulary")
flags.DEFINE_integer("batch_size", 32, "The size of batch images")
flags.DEFINE_integer("embed_size", 300, "Embed size")
flags.DEFINE_integer("hidden_size", 256, "Hidden dimension")
flags.DEFINE_integer("atten_layer", 3, "Num of attention layer")
flags.DEFINE_float("learning_rate", 3e-4, "Learning rate")
# flags.DEFINE_float("momentum", 0.9, "Momentum of RMSProp [0.9]")
# flags.DEFINE_float("decay", 0.95, "Decay of RMSProp [0.95]")
flags.DEFINE_float("dropout", 0.8, "Dropout rate")
flags.DEFINE_float("l2_rate", 0.0, "l2 regularization rate")
flags.DEFINE_float("clip_norm", 6 , "l2 regularization rate")
flags.DEFINE_string("optim", 'Adam', "The optimizer to use")
flags.DEFINE_string("atten", 'concat', "Attention Method")
flags.DEFINE_string("model", 'bow', "Model")
flags.DEFINE_string("init", 'ort', "xav, ort, non, ran")
flags.DEFINE_boolean("glove", False, "whether use glove embedding")
flags.DEFINE_boolean("tg", False, "whether train glove embedding")



FLAGS = flags.FLAGS

# less often changed parameters
sN = 10
sL = 50
qL = 15
stop_id = 2
val_rate = 0.05
glove_dir = './data/glove_wiki'

def initialize(sess, saver, load_path=None):
    if not load_path:
        sess.run(tf.initialize_all_variables())
    else:
        if os.path.isdir(load_path):
            fname = tf.train.latest_checkpoint(
                os.path.join(load_path, 'ckpts'))
            assert fname is not None
        else:
            fname = load_path
        print "  Load from %s" % fname
        saver.restore(sess, fname)


def create_logger(track_dir, to_console=True):
    """tracking high accuracy prediction"""
    import logging
    fname = os.path.join(track_dir, 'tracking.log')
    logger = logging.getLogger('tracker')

    hdlr = logging.FileHandler(fname)
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    hdlr.setFormatter(formatter)
    logger.addHandler(hdlr)

    if to_console:
        stdout = logging.StreamHandler()
        formatter = logging.Formatter('  %(levelname)s %(message)s')
        stdout.setFormatter(formatter)

    logger.addHandler(stdout)
    logger.setLevel(logging.DEBUG)

    return logger

def create_model(FLAGS, sN=sN, sL=sL, qL=qL):
    
    if FLAGS.model == 'bow':
        from bow_model import BoW_Attention as Net
    elif FLAGS.model == 'rr':
        from rrnn_model import RRNN_Attention as Net
    elif FLAGS.model == 'test':
        from test_model import Attention as Net
    else:
        raise ValueError(FLAGS.model)

    if FLAGS.init == 'non':
        initializer = None
    elif FLAGS.init == 'xav':
        initializer = tf.contrib.layers.xavier_initializer()
    elif FLAGS.init == 'ran':
        initializer=tf.truncated_normal_initializer(stddev=0.01)
    elif FLAGS.init == 'ort':
        initializer=orthogonal_initializer()

    l2 = l2_regularizer(FLAGS.l2_rate)
    def reg(w):
        if w.name.endswith('B:0') or w.name.endswith('Bias:0') or w.name.endswith('emb:0'):
            print 'ignoring %s'%w.name
            return tf.constant(0.0, dtype=tf.float32)

        print 'l2 to ', w.name
        return l2(w)

    with tf.variable_scope('model', initializer=initializer, regularizer = reg):

        model = Net(FLAGS.batch_size, sN, sL, qL, FLAGS.vocab_size, FLAGS.embed_size, FLAGS.hidden_size,
                    learning_rate=FLAGS.learning_rate,
                    optim_type=FLAGS.optim,
                    attention_type=FLAGS.atten,
                    attention_layer=FLAGS.atten_layer,
                    glove=FLAGS.glove,
                    train_glove=FLAGS.tg,
                    max_norm=FLAGS.clip_norm,
                    )

    return model


def main(_):

    w = FLAGS.weight
    if FLAGS.dataset == 'squad':

        data_dir = './data/squad'

        if not FLAGS.vocab_size: 
            FLAGS.__flags['vocab_size'] = 70000
            
        if not FLAGS.glove:
            data_path = './data/squad/ids_not_glove%d_train.txt' % FLAGS.vocab_size
        else:
            data_path = './data/squad/ids_glove%d_train.txt' % FLAGS.vocab_size

        if w == 'one':
            wt_path = './data/squad/train_ones.txt'
        elif w == 'idf':
            wt_path = './data/squad/train_idf.txt'
        elif w == 'tfidf':
            wt_path = './data/squad/train_tfidf.txt'

    elif FLAGS.dataset == 'nqa' or FLAGS.dataset == 'newsqa':

        data_dir = './data/squad'

        if not FLAGS.vocab_size: 
            FLAGS.__flags['vocab_size'] = 75000
            
        if not FLAGS.glove:
            data_path = './data/newsqa/ids_not_glove%d_train.txt' % FLAGS.vocab_size
        else:
            data_path = './data/newsqa/ids_glove%d_train.txt' % FLAGS.vocab_size

        if w == 'one':
            wt_path = './data/newsqa/train_ones.txt'
        elif w == 'idf':
            wt_path = './data/newsqa/train_idf.txt'
        elif w == 'tfidf':
            wt_path = './data/newsqa/train_tfidf.txt'

    else:
        raise ValueError(FLAGS.dataset)

    assert FLAGS.vocab_size!=0, FLAGS.__flags
    pp.pprint(FLAGS.__flags)

    go = raw_input('Do you want to go with these setting? ')
    if go not in ['Yes', 'y', 'Y', 'yes']:
        exit(2)

    if FLAGS.gpu is not None:
        gpu_list  = define_gpu(FLAGS.gpu)
        print('  Using GPU:%s' % gpu_list)
        # os.environ['CUDA_VISIBLE_DEVICES'] = str(FLAGS.gpu)

    with tf.Session() as sess:
        model = create_model(FLAGS)
        print '  Model Built'

        saver = tf.train.Saver(max_to_keep=5)
        initialize(sess, saver, FLAGS.load_path)
        print '  Variable inited'

        if FLAGS.glove:
            fname = os.path.join(glove_dir, 'glove.6B.%dd.txt' % FLAGS.embed_size)
            vocab_path = os.path.join(
                data_dir, "vocab_glove_%d.js" % FLAGS.vocab_size)
            with open(vocab_path, 'r') as f:
                vocab = json.load(f)
            embedding = restruct_glove_embedding(
                fname, vocab, dim=FLAGS.embed_size)
            sess.run(model.emb.assign(embedding))
            print '  Load Embedding Matrix from %s' % fname

        # load data =========================
        data = prepare_data(data_path, wt_path, 
                            data_size=FLAGS.data_size, val_rate=val_rate)
        train_data, train_wt, validate_data, validate_wt, vsize = data

        print '  Data Loaded from %s' % data_path
        print '  Weight Loaded from %s' % wt_path

        # log ================================
        log_dir = "%s/%s" % (FLAGS.log_dir, time.strftime("%m_%d_%H_%M"))
        save_dir = os.path.join(log_dir, 'ckpts')
        if os.path.exists(log_dir):
            print('log_dir exist %s' % log_dir)
            exit(2)
        os.makedirs(save_dir)
        with open(log_dir + '/Flags.js', 'w') as f:
            json.dump(FLAGS.__flags, f, indent=4)
        print '  Writing log to %s' % log_dir

        vcounter = 1
        writer = tf.train.SummaryWriter(log_dir, sess.graph)
        start_time = time.time()
        running_acc = 0.0
        running_loss = 0.0

        print '  Start Training'
        # tracker.info('  So you know I am working:)')
        sys.stdout.flush()
        for epoch_idx in range(FLAGS.epoch):

            order = range(len(train_data))
            np.random.shuffle(order)
            t_data = [ train_data[i] for i in order ]
            t_wt  = [ train_wt[i]  for i in order ]
            
            titer = batchIter(FLAGS.batch_size, t_data, t_wt,
                              sN, sL, qL, stop_id=stop_id, add_stop=False)
            tstep = titer.next()

            for batch_idx, P, p_wt, p_len, Q, q_wt, q_len, A in titer:

                rslt = sess.run(
                    [
                        model.global_step,
                        model.loss,
                        model.accuracy,
                        model.train_op,
                        model.train_summary, 

                        model.score,
                        model.alignment,
                        # model.origin_gv,

                        # model.check_op,
                        # model.mask_print,
                        # model.sn_c_print,

                    ],
                    feed_dict={
                        model.passage: P,
                        model.p_len: p_len,
                        model.p_wt: p_wt,
                        model.query: Q,
                        model.q_len: q_len,
                        model.q_wt: q_wt,
                        model.answer: A,
                        model.dropout: FLAGS.dropout,
                    })

                gstep, loss, accuracy, _, sum_str = rslt[:5]
                rslt = rslt[5:]

                score, align = rslt[:2]
                rslt = rslt[2:]

                loss = loss.mean()
                running_acc += accuracy
                running_loss += loss
                writer.add_summary(sum_str, gstep)


                if (gstep + 1) % 20 == 0:
                    print "%d Epoch: [%2d] [%4d/%4d] time: %4.4f, loss: %.8f, accuracy: %.8f" \
                        % (gstep, epoch_idx, batch_idx, tstep, time.time() - start_time, running_loss / 20.0, running_acc / 20.0)
                    sys.stdout.flush()
                    running_loss = 0.0
                    running_acc = 0.0
                    # sess.run(model.learning_rate)

                if (gstep + 1) % FLAGS.save_every == 0:
                    fname = os.path.join(save_dir, 'model')
                    print "  Saving Model..."
                    saver.save(sess, fname, global_step=gstep)

                if (gstep + 1) % FLAGS.eval_every == 0:

                    _accuracy = 0.0
                    _loss = 0.0
                    idxs = np.random.choice(len(validate_data), size=vsize)
                    D = [validate_data[idx] for idx in idxs]
                    W = [validate_wt[idx]  for idx in idxs]
                    viter = batchIter(FLAGS.batch_size, D, W,
                                sN, sL, qL, stop_id=stop_id, add_stop=False)
                    vstep = float(viter.next())

                    for batch_idx, P, p_wt, p_len, Q, q_wt, q_len, A in viter:
                        loss, accuracy, sum_str = sess.run(
                            [model.loss, model.accuracy, model.validate_summary],
                            feed_dict={
                                model.passage: P,
                                model.p_len: p_len,
                                model.p_wt: p_wt,
                                model.query: Q,
                                model.q_len: q_len,
                                model.q_wt: q_wt,
                                model.answer: A,
                                model.dropout: 1.0,
                            })

                        loss = loss.mean()
                        _accuracy += accuracy
                        _loss += loss
                        vcounter += 1
                        writer.add_summary(sum_str, vcounter)

                    print '  Evaluation: time: %4.4f, loss: %.8f, accuracy: %.8f' % \
                        (time.time() - start_time, _loss / vstep, _accuracy / vstep)
                    vcounter += int(vstep / 4.0)  # add gap



if __name__ == '__main__':
    tf.app.run()
    # try:
    #     tf.app.run()
    # except Exception, e:
    #     import ipdb, traceback
    #     etype, value, tb = sys.exc_info()
    #     traceback.print_exc()
    #     ipdb.post_mortem(tb)
