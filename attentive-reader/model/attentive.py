import time
import os
import numpy as np
import tensorflow as tf
from tensorflow.python.ops import rnn_cell

# from utils import array_pad
from base_model import Model
# from cells import LSTMCell, MultiRNNCellWithSkipConn
from data_utils import load_dataset


class AttentiveReader(Model):
    """Attentive Reader."""

    def __init__(self, size=128, vocab_size=264588,
                 learning_rate=1e-4, batch_size=32,
                 dropout=0.1, max_time_unit=100,
                 max_nsteps=1000,
                 max_query_length=50,
                 ):
        """Initialize the parameters for an  Attentive Reader model.

        Args:
          vocab_size: int, The dimensionality of the input vocab
          size: int, The dimensionality of the inputs into the Deep LSTM cell [32, 64, 256]
          learning_rate: float, [1e-3, 5e-4, 1e-4, 5e-5]
          batch_size: int, The size of a batch [16, 32]
          dropout: unit Tensor or float between 0 and 1 [0.0, 0.1, 0.2]
          max_time_unit: int, The max time unit [100]
        """
        super(AttentiveReader, self).__init__()

        self.size = size
        self.learning_rate = learning_rate
        self.batch_size = batch_size
        self.dropout = dropout
        self.max_nsteps = max_nsteps
        self.max_query_length = max_query_length
        self.vocab_size = vocab_size

        self.saver = None

    def prepare_model(self):

        self.document = tf.placeholder(
            tf.int32, [self.batch_size, self.max_nsteps])
        self.query = tf.placeholder(
            tf.int32, [self.batch_size, self.max_query_length])
        self.y = tf.placeholder(tf.float32, [self.batch_size, self.vocab_size])

        # Embeding
        self.emb = tf.get_variable("emb", [self.vocab_size, self.size])
        # shape: sentence_length, batch_size, embedding_size
        embed_d = tf.nn.embedding_lookup(self.emb, tf.transpose(self.document))
        # shape: sentence_length, batch_size, embedding_size
        embed_q = tf.nn.embedding_lookup(self.emb, tf.transpose(self.query))
        embed_d, embed_q = tf.unpack(embed_d), tf.unpack(embed_q)
        tf.histogram_summary("embed", self.emb)

        # representation
        with tf.variable_scope("document_represent"):
            d_t, d_foward_final_state, d_backward_final_state, = tf.nn.bidirectional_rnn(
                rnn_cell.BasicLSTMCell(
                    self.size, forget_bias=0.0, state_is_tuple=True),
                rnn_cell.BasicLSTMCell(
                    self.size, forget_bias=0.0, state_is_tuple=True),
                embed_d, dtype=tf.float32)
        with tf.variable_scope("query_represent"):
            q_t, q_foward_final_state, q_backward_final_state, = tf.nn.bidirectional_rnn(
                rnn_cell.BasicLSTMCell(
                    self.size, forget_bias=0.0, state_is_tuple=True),
                rnn_cell.BasicLSTMCell(
                    self.size, forget_bias=0.0, state_is_tuple=True),
                embed_q, dtype=tf.float32)

        _, q_0_b = tf.split(1, 2, q_t[0])
        q_T_f, _ = tf.split(1, 2, q_t[-1])
        u = tf.concat(1, [q_T_f, q_0_b])

        # attention
        W_ym = tf.get_variable('W_ym', [2 * self.size, 1])
        W_um = tf.get_variable('W_um', [2 * self.size, 1])
        m_t = []
        for d in d_t:
            m_cur = tf.tanh(tf.matmul(d, W_ym) + tf.matmul(u, W_um))
            m_t.append(m_cur)
        m = tf.concat(1, m_t)  # N,T
        s = tf.expand_dims(tf.nn.softmax(m), -1)  # N,T,1
        d = tf.pack(d_t, axis=1)  # N,T,2E
        r = tf.reduce_sum(s * d, 1)  # N,2E

        # predict
        W_rg = tf.get_variable("W_rg", [2 * self.size, self.vocab_size])
        W_ug = tf.get_variable("W_ug", [2 * self.size, self.vocab_size])
        mid = tf.matmul(r, W_rg) + tf.matmul(u, W_ug)
        # mid = tf.contrib.layers.batch_norm(mid)
        # g = tf.tanh(mid)
        g = tf.nn.relu(mid)
        tf.histogram_summary( 'before tanh', tf.reduce_mean(mid))

        self.loss = tf.nn.softmax_cross_entropy_with_logits(g, self.y, name='loss')
        tf.scalar_summary("loss", tf.reduce_sum(self.loss))

        correct_prediction = tf.equal(tf.argmax(self.y, 1), tf.argmax(g, 1))
        self.accuracy = tf.reduce_mean(tf.cast(correct_prediction, "float"), name='accuracy')
        tf.scalar_summary("accuracy", self.accuracy)

        print(" [*] Preparing model finished.")

    def train(self, sess, vocab_size, epoch=25, learning_rate=0.0002,
              momentum=0.9, decay=0.95, data_dir="data", dataset_name="cnn",
              log_dir='log/tmp/', load_path=None):

        print(" [*] Building Network...")
        self.prepare_model()

        # Creat Loss Function
        start = time.clock()
        print(" [*] Calculating gradient and loss...")
        self.optim = tf.train.AdamOptimizer(learning_rate, 0.9, name='optimizer')
        self.grad_and_var = self.optim.compute_gradients(self.loss)
        for g, v in self.grad_and_var:
            if g is not None:
                tf.scalar_summary( "{}-grad/mean".format(v.name), tf.reduce_mean(g) )
                tf.scalar_summary( "{}-var/mean".format(v.name), tf.reduce_mean(v) )
                # tf.histogram_summary("{}-variable".format(v.name),v)
        self.train_op = self.optim.apply_gradients(self.grad_and_var, name='train_op')

        self.vname = [ v.name for g,v in self.grad_and_var ]
        self.vars  = [ v for g,v in self.grad_and_var ]
        self.gras  = [ g for g,v in self.grad_and_var ]

        print(" [*] Calculating gradient and loss finished. Take %.2fs" % (time.clock() - start))

        # Summary
        merged = tf.merge_all_summaries()
        writer = tf.train.SummaryWriter(log_dir, sess.graph)

        # Saver and Load
        self.saver = tf.train.Saver()
        if load_path is not None:
            fname = tf.train.latest_checkpoint(os.path.join(load_path, 'ckpts'))
            self.saver.restore(sess, fname)
            print(" [*] Checkpoint is loaded.")

        sess.run(tf.initialize_all_variables())

        counter = 0
        start_time = time.time()
        ACC = []
        LOSS = []
        # var_log = open( os.path.join(log_dir, 'vars.log'),'w')
        for epoch_idx in xrange(epoch):
            train_iter, tsteps, validate_iter, vsteps = load_dataset(data_dir, dataset_name, \
                                        vocab_size, self.batch_size, self.max_nsteps, self.max_query_length, size=3000)
            
            # train
            for batch_idx, docs, queries, y in train_iter:
                _, summary_str, cost, accuracy, vars = sess.run([self.train_op, merged, self.loss, self.accuracy, self.vars ],
                                                      feed_dict={self.document: docs,
                                                                 self.query: queries,
                                                                 self.y: y}) 
                # for i in range(len(self.vname)):
                    # tmp = np.mean( vars[i] )
                    # if tmp is not None:



                writer.add_summary(summary_str, counter)
                if counter % 10 == 0:
                    print("Epoch: [%2d] [%4d/%4d] time: %4.4f, loss: %.8f, accuracy: %.8f"
                        % (epoch_idx, batch_idx, tsteps, time.time() - start_time, np.mean(cost), accuracy))
                counter += 1

            # validate
            running_acc = 0
            running_loss = 0 
            for batch_idx, docs, queries, y in validate_iter:
                cost, accuracy = sess.run([self.loss, self.accuracy],
                                          feed_dict={self.document: docs,
                                                     self.query: queries,
                                                     self.y: y})
                running_acc += accuracy
                running_loss += np.mean(cost)
            ACC.append(running_acc/vsteps)
            LOSS.append(running_loss/vsteps)
            print("Epoch: [%2d] Validation time: %4.4f, loss: %.8f, accuracy: %.8f\n\n"
                      %(epoch_idx, time.time()-start_time, running_loss/vsteps, running_acc/vsteps))

        if epoch_idx % 5 == 0:
            self.save(sess, log_dir, dataset_name, global_step=counter)

    def save(self, sess, log_dir, dataset_name, global_step=None):
        assert self.saver is not None
        print(" [*] Saving checkpoints...")
        checkpoint_dir = os.path.join(log_dir, "ckpts")
        if not os.path.exists(checkpoint_dir):
            os.makedirs(checkpoint_dir)
        fname = os.path.join(checkpoint_dir, 'model')
        self.saver.save(sess, fname, global_step=global_step)

    def test(self, voab_size):
        pass
        # self.prepare_model(data_dir, dataset_name, vocab_size)