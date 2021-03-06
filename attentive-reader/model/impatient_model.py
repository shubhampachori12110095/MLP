import time
import os
import numpy as np
import tensorflow as tf
from tensorflow.python.ops import rnn_cell
from utils import load_dataset, fetch_files, data_iter

class ImpatientReader():

    def __init__(self, vocab_size=50003, batch_size=32,
                 learning_rate=1e-4, momentum=0.9, decay=0.95, l2_rate=1e-4,
                 size=256,
                 max_nsteps=1000,
                 max_query_length=20,
                 use_optimizer='RMS',
                 activation='tanh',
                 attention='bilinear',
                 bidirection=True,
                 D=25,
                 ):

        self.size = size
        self.learning_rate = learning_rate
        self.batch_size = batch_size
        self.max_nsteps = max_nsteps
        self.max_query_length = max_query_length
        self.vocab_size = vocab_size
        # self.dropout = dropout_rate
        self.l2_rate = l2_rate
        self.momentum = momentum
        self.decay = decay
        self.use_optimizer = use_optimizer
        self.activation = activation
        self.attention = attention
        self.bidirection = bidirection
        self.D = D

        self.saver = None

    def prepare_model(self, parallel=False):

        self.document = tf.placeholder(
            tf.int32, [self.batch_size, self.max_nsteps], name='document')
        self.query = tf.placeholder(
            tf.int32, [self.batch_size, self.max_query_length], name='query')
        self.d_end = tf.placeholder(tf.int32, self.batch_size, name='docu-end')
        self.q_end = tf.placeholder(tf.int32, self.batch_size, name='quer-end')
        self.y = tf.placeholder(
            tf.float32, [self.batch_size, self.vocab_size], name='Y')
        self.dropout = tf.placeholder(tf.float32, name='dropout_rate')

        # Embeding
        self.emb = tf.get_variable("emb", [self.vocab_size, self.size])
        embed_d = tf.nn.embedding_lookup(self.emb, self.document, name='embed_d')
        embed_q = tf.nn.embedding_lookup(self.emb, self.query, name='embed_q')
        embed_sum = tf.histogram_summary("embed", self.emb)

        # embed_d = tf.nn.dropout(embed_d, keep_prob=self.dropout)
        # embed_q = tf.nn.dropout(embed_q, keep_prob=self.dropout)

        # representation
        with tf.variable_scope("document_represent"):
            # d_t: N, T, Hidden
            d_t, d_final_state = self.rnn(embed_d, self.d_end, use_bidirection=self.bidirection)
            d_t = tf.concat(2, d_t)
            print d_t.get_shape()
            d_t = tf.nn.dropout(d_t, keep_prob=self.dropout)

        with tf.variable_scope("query_represent"):
            q_t, q_final_state = self.rnn(embed_q, self.q_end, use_bidirection=self.bidirection)

            print type(q_t)

            if self.bidirection:
                q_f = tf.unpack(q_t[0], axis=1)
                q_b = tf.unpack(q_t[-1], axis=1)
                print q_f[-1].get_shape()
                print q_b[0].get_shape()
                u = tf.concat(1, [q_f[-1], q_b[0]], name='u')  # N, Hidden*2
            else:
                u = tf.unpack(q_t, axis=1)[-1]

            u = tf.nn.dropout(u, keep_prob=self.dropout)


        self.d_t = d_t
        self.u = u

        # attention
        def attention_function():

            if self.attention == 'concat':
                return self.concat_attention
            elif self.attention == 'bilinear':
                return self.bilinear_attention
            elif self.attention == 'local':
                # r = self.local_attention(d_t, u, attention='concat')
                from attention import local_attention
                content_func = lambda x, y : self.concat_attention(x,y, return_attention=True)
                r, atten_hist = local_attention(u, d_t, window_size=self.D, content_function=content_func)
            else:
                raise ValueError(self.attention)

        # predict
        W_rg = tf.get_variable("W_rg", [2 * self.size, self.size])
        W_ug = tf.get_variable("W_ug", [2 * self.size, self.size])
        W_g = tf.get_variable('W_g', [self.size, self.vocab_size])
        mid = tf.matmul(r, W_rg, name='r_x_W') + \
            tf.matmul(u, W_ug, name='u_x_W')
        if self.activation == 'relu':
            g = tf.nn.relu(mid, name='relu_g')
        elif self.activation == 'tanh':
            g = tf.tanh(mid, name='g')
        elif self.activation == 'none':
            g = mid
        else:
            raise ValueError(self.activation)

        g = tf.nn.dropout(g, keep_prob=self.dropout)
        g = tf.matmul(g, W_g, name='g_x_W')
        self.score = g

        beact_sum = tf.scalar_summary(
            'before activitation', tf.reduce_mean(mid))
        afact_sum = tf.scalar_summary(
            'before activitation_after', tf.reduce_mean(g))

        self.loss = tf.nn.softmax_cross_entropy_with_logits(
            g, self.y, name='loss')
        if self.l2_rate > 0:
            for v in tf.trainable_variables():
                if v.name.endswith('Matrix:0') or v.name.startswith('W'):
                    self.loss += self.l2_rate * \
                        tf.nn.l2_loss(v, name="%s-l2loss" % v.name[:-2])
        loss_sum = tf.scalar_summary("T_loss", tf.reduce_mean(self.loss))

        correct_prediction = tf.equal(tf.argmax(self.y, 1), tf.argmax(g, 1))
        self.accuracy = tf.reduce_mean(
            tf.cast(correct_prediction, "float"), name='accuracy')
        acc_sum = tf.scalar_summary("T_accuracy", self.accuracy)

        # optimize
        self.optim = self.get_optimizer()

        self.grad_and_var = self.optim.compute_gradients(self.loss)
        if not parallel:
            self.train_op = self.optim.apply_gradients(
                self.grad_and_var, name='train_op')
        else:
            self.train_op = None

        # train_sum
        gv_sum = []
        for g, v in self.grad_and_var:
            v_sum = tf.scalar_summary(
                "I_{}-var/mean".format(v.name), tf.reduce_mean(v))
            gv_sum.append(v_sum)
            if g is not None:
                g_sum = tf.scalar_summary(
                    "I_{}-grad/mean".format(v.name), tf.reduce_mean(g))
                gv_sum.append(g_sum)

        self.vname = [v.name for g, v in self.grad_and_var]
        self.vars = [v for g, v in self.grad_and_var]
        self.gras = [g for g, v in self.grad_and_var]
        self.train_sum = tf.merge_summary([loss_sum, acc_sum, atten_hist])

        # validation sum
        v_loss_sum = tf.scalar_summary("V_loss", tf.reduce_mean(self.loss))
        v_acc_sum = tf.scalar_summary("V_accuracy", self.accuracy)
        self.validate_sum = tf.merge_summary(
            [embed_sum, v_loss_sum, v_acc_sum])

        # import ipdb; ipdb.set_trace()

    def rnn(self, input_tensor, seq_length, dtype=tf.float32, use_bidirection=True, cell_type='LSTM'):

        if cell_type == 'LSTM':
            cell = lambda size: rnn_cell.LSTMCell(size, state_is_tuple=True)
        elif cell_type == 'GRU':
            cell = rnn_cell.GRU
        else:
            raise ValueError(cell_type)

        if use_bidirection:            
            # (TODO) change
            h_t, final_state, = tf.nn.bidirectional_dynamic_rnn(
                cell(self.size),
                cell(self.size),
                input_tensor,
                sequence_length=seq_length, dtype=dtype)

        else:
            h_t, final_state = tf.nn.dynamic_rnn(
                cell(2 * self.size),
                input_tensor,
                sequence_length=seq_length, dtype=dtype)

        return h_t, final_state

    def get_optimizer(self):
        if self.use_optimizer == 'SGD':
            optim = tf.train.GradientDescentOptimizer(
                self.learning_rate, name='optimizer')
        elif self.use_optimizer == 'Adam':
            optim = tf.train.AdamOptimizer(
                self.learning_rate, name='optimizer')
        elif self.use_optimizer == 'RMS':
            optim = tf.train.RMSPropOptimizer(
                self.learning_rate, momentum=self.momentum, decay=self.decay, name='optimizer')
        return optim


    def concat_attention(self, d_t, u, return_attention=False):
        W_ym = tf.get_variable('W_ym', [2 * self.size, self.size])
        W_um = tf.get_variable('W_um', [2 * self.size, self.size])
        W_ms = tf.get_variable('W_ms', [self.size])
        m_t = []
        U = tf.matmul(u, W_um)  # N,H

        d_t = tf.unpack(d_t, axis=1)
        for d in d_t:
            m_t.append(tf.matmul(d, W_ym) + U)  # N,H
        m = tf.pack(m_t, 1)  # N,T,H
        m = tf.tanh(m)
        ms = tf.reduce_sum(m * W_ms, 2, keep_dims=True, name='ms')  # N,T,1
        s = tf.nn.softmax(ms, 1)  # N,T,1
        self.attention = tf.squeeze(s, [-1], name='attention')
        d = tf.pack(d_t, axis=1)  # N,T,2E
        if return_attention:
            return self.attention
        else:
            r = tf.reduce_sum(s * d, 1, name='r')  # N, 2E
            return r

    def bilinear_attention(self, d_t, u, return_attention=False):
        W = tf.get_variable('W_bilinear', [2 * self.size, 2 * self.size])
        atten = []

        d_t = tf.unpack(d_t, axis=1)
        for d in d_t:
            a = tf.matmul(d, W, name='dW')  # N, 2H
            a = tf.reduce_sum(a * u, 1, name='Wq')  # N
            atten.append(a)
        atten = tf.pack(atten, axis=1, )  # N, T
        atten = tf.nn.softmax(atten, name='attention')
        self.attention = atten
        atten = tf.expand_dims(atten, 2)  # N, T, 1
        d = tf.pack(d_t, axis=1)
        if return_attention:
            return self.attention
        else:
            r = tf.reduce_sum(atten * d, 1, name='r')
            return r

    def local_attention(self, d_t, u, attention='bilinear'):

        Wp = tf.get_variable('Wp', [2 * self.size, 2 * self.size])
        V = tf.get_variable('V', [2 * self.size])
        D = self.D

        tanh = tf.tanh(tf.matmul(u, Wp))
        p = tf.reduce_sum(tanh * V, 1)  # N
        p = self.max_nsteps * tf.sigmoid(p)
        p = tf.to_int32(tf.floor(p))
        self.p = p

        pt = tf.minimum(p, D)
        pt = tf.maximum(pt, self.max_nsteps - D - 1)
        begin_idx = pt - D  # N
        zero = tf.constant(0, shape=[self.batch_size], dtype=tf.int32)
        begin = tf.pack([begin_idx, zero], 1)  # N, 2 of [p-D, 0]
        size = tf.constant([2 * D, -1], dtype=tf.int32)

        with tf.name_scope('attention_extract'):
            batches = []  # N * [ 2D*2H ]
            begin = tf.unpack(begin)
            d_t = tf.unpack(d_t)
            for b, d in zip(begin, d_t):
                block = tf.slice(d, b, size)
                batches.append(block)

            batches = tf.pack(batches)

        if attention == 'bilinear':
            alignment = self.bilinear_attention(
                batches, u, return_attention=True)  # N, 2D, 2H
        elif attention == 'concat':
            alignment = self.concat_attention(
                batches, u, return_attention=True)
        else:
            raise ValueError(attention)

        # here we calculate the 'truncated normal distribution'
        idx = [begin_idx + i for i in range(2 * D)]  # [N]*2D
        idx = tf.pack(idx, 1)  # N, 2D

        denominator = (D / 2.0) ** 2.0
        numerator = -tf.pow(tf.to_float((idx - tf.expand_dims(p, 1))), 2.0)
        div = tf.truediv(numerator, denominator)
        e = tf.exp(div)  # result of the truncated normal distribution

        self.attention = tf.mul(alignment, e, name='local_attention')  # N, 2D
        r = tf.reduce_sum(
            batches * tf.expand_dims(self.attention, -1), 1, name='r')
        return r

    def train(self, sess, vocab_size, epoch=25, data_dir="data", dataset_name="cnn",
              log_dir='log/tmp/', load_path=None, data_size=3000, eval_every=1500, val_rate=0.1, dropout_rate=0.9):

        print(" [*] Building Network...")
        start = time.time()
        self.prepare_model()
        print(" [*] Preparing model finished. Use %4.4f" %
              (time.time() - start))

        # Summary
        writer = tf.train.SummaryWriter(log_dir, sess.graph)
        print(" [*] Writing log to %s" % log_dir)

        # Saver and Load
        self.saver = tf.train.Saver(max_to_keep=15)
        if load_path is not None:
            if os.path.isdir(load_path):
                fname = tf.train.latest_checkpoint(
                    os.path.join(load_path, 'ckpts'))
                assert fname is not None
            else:
                fname = load_path

            print(" [*] Loading %s" % fname)
            self.saver.restore(sess, fname)
            print(" [*] Checkpoint is loaded.")
        else:
            sess.run(tf.initialize_all_variables())
            print(" [*] No checkpoint to load, all variable inited")

        counter = 0
        vcounter = 0
        start_time = time.time()
        ACC = []
        LOSS = []
        train_files, validate_files = fetch_files(
            data_dir, dataset_name, vocab_size)
        if data_size:
            train_files = train_files[:data_size]
        validate_size = int(
            min(max(20.0, float(len(train_files)) * val_rate), len(validate_files)))
        print(" [*] Validate_size %d" % validate_size)

        for epoch_idx in xrange(epoch):
            # load data
            train_iter = data_iter(train_files, self.max_nsteps, self.max_query_length,
                                   batch_size=self.batch_size,
                                   vocab_size=self.vocab_size,
                                   shuffle_data=True)
            tsteps = train_iter.next()

            # train
            running_acc = 0
            running_loss = 0
            for batch_idx, docs, d_end, queries, q_end, y in train_iter:
                _, summary_str, cost, accuracy = sess.run([self.train_op, self.train_sum, self.loss, self.accuracy],
                                                          feed_dict={self.document: docs,
                                                                     self.query: queries,
                                                                     self.d_end: d_end,
                                                                     self.q_end: q_end,
                                                                     self.y: y,
                                                                     self.dropout: dropout_rate,
                                                                     })

                writer.add_summary(summary_str, counter)
                running_acc += accuracy
                running_loss += np.mean(cost)
                if counter % 10 == 0:
                    print("Epoch: [%2d] [%4d/%4d] time: %4.4f, loss: %.8f, accuracy: %.8f"
                          % (epoch_idx, batch_idx, tsteps, time.time() - start_time, running_loss / 10.0, running_acc / 10.0))
                    running_loss = 0
                    running_acc = 0
                counter += 1

                if (counter + 1) % eval_every == 0:
                    # validate
                    running_acc = 0
                    running_loss = 0

                    idxs = np.random.choice(
                        len(validate_files), size=validate_size)
                    files = [validate_files[idx] for idx in idxs]
                    validate_iter = data_iter(files, self.max_nsteps, self.max_query_length,
                                              batch_size=self.batch_size,
                                              vocab_size=self.vocab_size,
                                              shuffle_data=True)
                    vsteps = validate_iter.next()

                    for batch_idx, docs, d_end, queries, q_end, y in validate_iter:
                        validate_sum_str, cost, accuracy = sess.run([self.validate_sum, self.loss, self.accuracy],
                                                                    feed_dict={self.document: docs,
                                                                               self.query: queries,
                                                                               self.d_end: d_end,
                                                                               self.q_end: q_end,
                                                                               self.y: y,
                                                                               self.dropout: 1.0,
                                                                               })
                        writer.add_summary(validate_sum_str, vcounter)
                        running_acc += accuracy
                        running_loss += np.mean(cost)
                        vcounter += 1

                    ACC.append(running_acc / vsteps)
                    LOSS.append(running_loss / vsteps)
                    vcounter += vsteps
                    print("Epoch: [%2d] Validation time: %4.4f, loss: %.8f, accuracy: %.8f"
                          % (epoch_idx, time.time() - start_time, running_loss / vsteps, running_acc / vsteps))

                    # save
                    self.save(sess, log_dir, global_step=counter)

            print('\n\n')

    def save(self, sess, log_dir, global_step=None):
        assert self.saver is not None
        print(" [*] Saving checkpoints...")
        checkpoint_dir = os.path.join(log_dir, "ckpts")
        if not os.path.exists(checkpoint_dir):
            os.makedirs(checkpoint_dir)
        fname = os.path.join(checkpoint_dir, 'model')
        self.saver.save(sess, fname, global_step=global_step)