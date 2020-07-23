# -*- coding: utf-8 -*-
"""
Yizhe Zhang, Dinghan Shen, Guoyin Wang

TextCNN
"""

import os
import tensorflow as tf
from tensorflow.contrib import learn
from tensorflow.contrib import layers
from tensorflow.contrib import framework
from tensorflow.contrib.learn.python.learn import learn_runner
from tensorflow.python.platform import tf_logging as logging
import cPickle
import numpy as np
import os
import scipy.io as sio
from math import floor
import pdb
from model import *
from utils import prepare_data_for_cnn, prepare_data_for_rnn, \
    get_minibatches_idx, normalizing, restore_from_save, \
    prepare_for_bleu, cal_BLEU, sent2idx, _clip_gradients_seperate_norm
from denoise import *
from error_rate import prepare_for_cer, cal_cer

## 弄明白输入，输出
##字符集的

GPUID = 0
os.environ['CUDA_VISIBLE_DEVICES'] = str(GPUID)
profile = False

logging.set_verbosity(logging.INFO)
# Basic model parameters as external flags.
flags = tf.app.flags
FLAGS = flags.FLAGS


class Options(object):
    #类，一个init构造函数
    def __init__(self):
        # self.fix_emb = False
        self.reuse_w = False
        self.reuse_cnn = False
        self.reuse_discrimination = False  # reuse cnn for discrimination
        self.restore = False
        self.tanh = False  # activation fun for the top layer of cnn, otherwise relu
        self.model = 'cnn_deconv' #'cnn_deconv'  # 'cnn_rnn', 'rnn_rnn' , default: cnn_deconv

        self.permutation = 0.1
        self.substitution = 'sc'  # Deletion(d), Insertion(a), Substitution(s) and Permutation(p), c for char special

        self.W_emb = None
        self.cnn_W = None
        self.cnn_b = None
        self.maxlen = 221
        self.n_words = None
        self.filter_shape = 5
        self.filter_size = 300
        self.multiplier = 2 ##
        self.lr = 1e-4

        self.layer = 3
        self.stride = [2,2]   # for two layer cnn/deconv , use self.stride[0]
        self.batch_size = 64
        self.max_epochs = 50
        self.n_gan = 900  # self.filter_size * 3
        self.L = 50

        self.optimizer = 'Adam' #tf.train.AdamOptimizer(beta1=0.9) #'Adam' # 'Momentum' , 'RMSProp'
        self.clip_grad = None  #100  #  20#
        self.attentive_emb = False
        self.decay_rate = 0.99
        self.relu_w = True #

        self.save_path = "./save/" +str(self.n_gan) + "_dim_" + self.model + "_" + self.substitution + str(self.permutation)
        self.log_path = "./log"

        self.print_freq = 1
        self.valid_freq = 1

        # batch norm & dropout
        self.batch_norm = False
        self.cnn_layer_dropout = False
        self.dropout = False
        self.dropout_ratio = 0.5

        self.discrimination = False

        self.H_dis = 300

        self.sent_len = self.maxlen + 2*(self.filter_shape-1)
        self.sent_len2 = np.int32(floor((self.sent_len - self.filter_shape)
                            / self.stride[0]) + 1)
        self.sent_len3 = np.int32(floor((self.sent_len2 - self.filter_shape)
                            / self.stride[1]) + 1)

        # add char label
        self.char = True
        # dataset label
        self.data = 'yahoo'  # option is three_small, three_char, imdb
        print('Use model %s' % self.model)
        print('Use %d conv/deconv layers' % self.layer)

    def __iter__(self):
        for attr, value in self.__dict__.iteritems():
            yield attr, value

def auto_encoder(x, x_org, is_train, opt, opt_t=None):
    ## auto_encoder(x_, x_org_, is_train_, opt)
    if not opt_t:
        opt_t = opt
    x_emb, W_norm = embedding(x, opt)   # batch L emb
    ###  网络输入   字向量文件
    x_emb = tf.expand_dims(x_emb, 3)   # batch L emb 1
    res = {}
    # cnn encoder

    H_enc, res = conv_encoder(x_emb, is_train, opt, res)
    ## 对res没有任何改变

    H_dec = H_enc

    if opt.model == 'rnn_rnn':
        loss, rec_sent_1, _ = seq2seq(x, x_org, opt)
        _, rec_sent_2, _ = seq2seq(x, x_org, opt, feed_previous=True, is_reuse=True)

        res['rec_sents_feed_y'] = rec_sent_1
        ###  这样保存有什么用意
        res['rec_sents'] = rec_sent_2


    elif opt.model == 'cnn_rnn':
        # lstm decoder
        H_dec2 = tf.identity(H_dec)
        loss, rec_sent_1, _ = lstm_decoder(H_dec, x_org, opt)  #

        _, rec_sent_2, _ = lstm_decoder(H_dec, x_org, opt, feed_previous=True, is_reuse=True)

        res['rec_sents_feed_y'] = rec_sent_1
        res['rec_sents'] = rec_sent_2
 

    else:

        # deconv decoder
        loss, res = deconv_decoder(H_dec, x_org, W_norm, is_train, opt_t, res)

    tf.summary.scalar('loss', loss)
    ## tf.summary.scalar('loss',loss)
    summaries = [
                "learning_rate",
                "loss",
                "gradients",
                "gradient_norm",
                ]

    global_step = tf.Variable(0, trainable=False)


    train_op = layers.optimize_loss(
        loss,
        global_step=global_step,
        #aggregation_method=tf.AggregationMethod.EXPERIMENTAL_ACCUMULATE_N,
        #framework.get_global_step(),
        optimizer=opt.optimizer,
        clip_gradients=(lambda grad: _clip_gradients_seperate_norm(grad, opt.clip_grad)) if opt.clip_grad else None,
        ##应该是梯度剪枝
        learning_rate_decay_fn=lambda lr,g: tf.train.exponential_decay(learning_rate=lr, global_step=g, decay_rate=opt.decay_rate, decay_steps=3000),
        ## 函数
        learning_rate=opt.lr,
        summaries=summaries
        )
        ### res ---> decoder得到的结果
    return res, loss, train_op


def run_model(opt, train, val, test, wordtoix, ixtoword):
    ##  run_model(opt, train, val, test, wordtoix, ixtoword)

    with tf.device('/gpu:1'):
       
        ## 输入原来的句子，输出纠正后的句子
        x_ = tf.placeholder(tf.int32, shape=[opt.batch_size, opt.sent_len])
        x_org_ = tf.placeholder(tf.int32, shape=[opt.batch_size, opt.sent_len])
        ## batchsize ** sequence_length 
        is_train_ = tf.placeholder(tf.bool, name='is_train_')
        
        res_, loss_, train_op = auto_encoder(x_, x_org_, is_train_, opt)
        ###  auto_encoder 包括了encoder和decoder的过程
        
        merged = tf.summary.merge_all()
        summary_ext = tf.Summary()

    
    uidx = 0
    config = tf.ConfigProto(log_device_placement=False,
                            allow_soft_placement=True,
                            graph_options=tf.GraphOptions(build_cost_model=1))
    config.gpu_options.allow_growth = True
    
    np.set_printoptions(precision=3)
    np.set_printoptions(threshold=np.inf) ## 不省略打印
    ## 设置输出精度
    
    saver = tf.train.Saver()

    run_metadata = tf.RunMetadata()

    with tf.Session(config=config) as sess:
        train_writer = tf.summary.FileWriter(opt.log_path + '/train', sess.graph)
        test_writer = tf.summary.FileWriter(opt.log_path + '/test', sess.graph)
        sess.run(tf.global_variables_initializer())
        
        if opt.restore:
            ##加载模型
            try:
                t_vars = tf.trainable_variables()
                loader = restore_from_save(t_vars, sess, opt)
                ####
            except Exception as e:
                print(e)
                print("No saving session, using random initialization")
                sess.run(tf.global_variables_initializer())
        
        ##原来跑的代码有什么问题，为什么结果那么奇怪
        newx=cPickle.load(open('./data/collect_data','rb'))
        Nx,Nx_org=newx[0],newx[1]
        print len(Nx)
        addlen=len(Nx)
        savetofile=open("GEC_re",'w')
     
        for epoch in range(opt.max_epochs):
            #100次迭代
            print("Starting epoch %d" % epoch)
            
            #kf = get_minibatches_idx(len(train), opt.batch_size // 2, shuffle=True)
            
            ks = get_minibatches_idx(int(addlen*0.9), opt.batch_size, shuffle=True)
            #print len(ks)
            for _, train_index in ks:
                #print "11111"
                uidx += 1
                sents = [Nx_org[t] for t in train_index]
                sents_permutated =[Nx[t] for t in train_index]
                
#                sents.extend(Nx_org[:int(addlen*0.8)])
#                sents_permutated.extend(Nx[:int(addlen*0.2)])
#                

                ## 随便选组模拟数据
                index = np.random.choice(len(train), opt.batch_size)
                ## 为了后面剔除掉一些
                sents_org = [train[t] for t in index]
                sents_permutated_org=add_noise(sents_org, opt)
                
                sents.extend(sents_org)
                sents_permutated.extend(sents_permutated_org)
                
#                shuff1=np.random.permutation(np.arange(len(sents)))
#                sents=np.array(sents)[shuff1]
#                shuff2=np.random.permutation(np.arange(len(sents_permutated)))
#                sents_permutated=np.array(sents_permutated)[shuff2]
#                print len(sents)
                
                if opt.model != 'rnn_rnn' and opt.model != 'cnn_rnn':
                    x_batch_org = prepare_data_for_cnn(sents, opt) # Batch L
                else:
                    x_batch_org = prepare_data_for_rnn(sents, opt) # Batch L
                    
                shuff1=np.random.permutation(np.arange(len(x_batch_org)))
                x_batch_org=x_batch_org[shuff1]

                
                if opt.model != 'rnn_rnn':
                    x_batch = prepare_data_for_cnn(sents_permutated, opt) # Batch L
                else:
                    x_batch = prepare_data_for_rnn(sents_permutated, opt, is_add_GO = False) # Batch L
                
                shuff2=np.random.permutation(np.arange(len(x_batch)))
                x_batch=x_batch_org[shuff2]
                
                
                # x_print = sess.run([x_emb],feed_dict={x_: x_train} )
                # print x_print


                # res = sess.run(res_, feed_dict={x_: x_batch, x_org_:x_batch_org})
                # pdb.set_trace()
                #print len(x_batch)

                if profile:
                    _, loss = sess.run([train_op, loss_], feed_dict={x_: x_batch, x_org_: x_batch_org, is_train_:1},options=tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE),run_metadata=run_metadata)
                else:
                    _, loss = sess.run([train_op, loss_], feed_dict={x_: x_batch, x_org_: x_batch_org, is_train_:1})

                #pdb.set_trace()

                if uidx % opt.valid_freq == 0:
                    is_train = None

                    val =Nx_org[int(addlen*0.1):]
                    val_permutated = Nx[int(addlen*0.1):]
                    
                    valid_index = np.random.choice(len(val), 3*opt.batch_size)
                    val_sents = [val[t] for t in valid_index]
                    val_sents_permutated= [val_permutated[t] for t in valid_index]
                    
                    if opt.model != 'rnn_rnn' and opt.model != 'cnn_rnn':
                        x_val_batch_org = prepare_data_for_cnn(val_sents, opt)
                    else:
                        x_val_batch_org = prepare_data_for_rnn(val_sents, opt)

                    if opt.model != 'rnn_rnn':
                        x_val_batch = prepare_data_for_cnn(val_sents_permutated, opt)
                    else:
                        x_val_batch = prepare_data_for_rnn(val_sents_permutated, opt, is_add_GO=False)

                    loss_val = sess.run(loss_, feed_dict={x_: x_val_batch, x_org_: x_val_batch_org, is_train_:is_train })
                    print("Validation loss %f " % (loss_val))
                    res = sess.run(res_, feed_dict={x_: x_val_batch, x_org_: x_val_batch_org, is_train_:is_train })
                    
                    
                    if opt.discrimination:
                        ## discrimination ???
                        print ("Real Prob %f Fake Prob %f"%(res['prob_r'], res['prob_f']))

                    if opt.char:
                        ## 0是填充的，所以不还原
                        ### val_sents[0]   一个batch只输出一句？
                        ### 准确率呢 loss呢
                        print "Val Orig :" + "".join([ixtoword[x] for x in val_sents[0] if x != 0])
                        print "Val Perm :" + "".join([ixtoword[x] for x in val_sents_permutated[0] if x != 0])
                        print "Val Recon:" + "".join([ixtoword[x] for x in res['rec_sents'][0] if x != 0])
                    
                    if uidx %50 == 0:
                        for i in range(len(x_val_batch)):
                           savetofile.writelines("Val Orig :" + "".join([ixtoword[x] for x in val_sents[i] if x != 0]))
                           savetofile.writelines('\n')
                           savetofile.writelines("Val Orig :" + "".join([ixtoword[x] for x in val_sents_permutated[i] if x != 0]))
                           savetofile.writelines('\n')
                           savetofile.writelines("Val Orig :" + "".join([ixtoword[x] for x in res['rec_sents'][i] if x != 0]))
                           savetofile.writelines('\n')
                           # print "Val Recon one hot:" + "".join([ixtoword[x] for x in res['rec_sents_one_hot'][0] if x != 0])
#                        
#                    else:
#                        print "Val Orig :" + " ".join([ixtoword[x] for x in val_sents[0] if x != 0])
#                        print "Val Perm :" + " ".join([ixtoword[x] for x in val_sents_permutated[0] if x != 0])
#                        print "Val Recon:" + " ".join([ixtoword[x] for x in res['rec_sents'][0] if x != 0])
#                      ## 有空格单词
#


                    val_set = [prepare_for_bleu(s) for s in val_sents]
                    [bleu2s,bleu3s,bleu4s] = cal_BLEU([prepare_for_bleu(s) for s in res['rec_sents']], {0: val_set})
                    print 'Val BLEU (2,3,4): ' + ' '.join([str(round(it, 3)) for it in (bleu2s,bleu3s,bleu4s)])


                    val_set_char = [prepare_for_cer(s, ixtoword) for s in val_sents]
                    cer = cal_cer([prepare_for_cer(s, ixtoword) for s in res['rec_sents']], val_set_char)
                    print 'Val CER: ' + str(round(cer, 3))
                    # summary_ext.Value(tag='CER', simple_value=cer)
                    summary_ext = tf.Summary(value=[tf.Summary.Value(tag='CER', simple_value=cer)])
                    # tf.summary.scalar('CER', cer)

                    #if opt.model != 'rnn_rnn' and opt.model != 'cnn_rnn':
                        #print "Gen Probs:" + " ".join([str(np.round(res['gen_p'][i], 1)) for i in range(len(res['rec_sents'][0])) if res['rec_sents'][0][i] != 0])
                    summary = sess.run(merged, feed_dict={x_: x_val_batch, x_org_: x_val_batch_org, is_train_:is_train })
                    test_writer.add_summary(summary, uidx)
                    test_writer.add_summary(summary_ext, uidx)
                    is_train = True

                
                if uidx%opt.print_freq == 0:
                    print("Iteration %d: loss %f " %(uidx, loss))
                    res = sess.run(res_, feed_dict={x_: x_batch, x_org_: x_batch_org, is_train_:1})

                    # if 1 in res['rec_sents'][0] or 1 in sents[0]:
                    #     pdb.set_trace()
                    if opt.char:
                        print "Original     :" + "".join([ixtoword[x] for x in sents[0] if x != 0])
                        print "Permutated   :" + "".join([ixtoword[x] for x in sents_permutated[0] if x != 0])
                        if opt.model == 'rnn_rnn' or opt.model == 'cnn_rnn':
                            print "Reconstructed:" + " ".join([ixtoword[x] for x in res['rec_sents_feed_y'][0] if x != 0])
                        print "Reconstructed:" + "".join([ixtoword[x] for x in res['rec_sents'][0] if x != 0])


                    else:
                        print "Original     :" + " ".join([ixtoword[x] for x in sents[0] if x != 0])
                        print "Permutated   :" + " ".join([ixtoword[x] for x in sents_permutated[0] if x != 0])
                        if opt.model == 'rnn_rnn' or opt.model == 'cnn_rnn':
                            print "Reconstructed:" + " ".join([ixtoword[x] for x in res['rec_sents_feed_y'][0] if x != 0])
                        print "Reconstructed:" + " ".join([ixtoword[x] for x in res['rec_sents'][0] if x != 0])


                    summary = sess.run(merged, feed_dict={x_: x_batch, x_org_: x_batch_org, is_train_:1})
                    train_writer.add_summary(summary, uidx)
                    # print res['x_rec'][0][0]
                    # print res['x_emb'][0][0]
                    if profile:
                        tf.contrib.tfprof.model_analyzer.print_model_analysis(
                        tf.get_default_graph(),
                        run_meta=run_metadata,
                        tfprof_options=tf.contrib.tfprof.model_analyzer.PRINT_ALL_TIMING_MEMORY)

            saver.save(sess, opt.save_path)
        savetofile.close()



def main():
   opt = Options()
    #参数类，实例化一个类
   if opt.char:
        ### 字符级别
        opt.n_words = 35      ## 字母个数
        opt.embed_size = 35   ## 字母维度
        opt.fix_emb = False   ## 字符向量在训练过程中是否保持不变
        opt.filter_size= 300  ##卷积核的个数

   if opt.data == 'three_char':
        loadpath = './data/three_corpus_correct_large_char.p'
   elif opt.data == 'yahoo':
        loadpath = './data/yahoo_char.p'

    # loadpath = "./data/three_corpus_corrected_large.p"
   x = cPickle.load(open(loadpath,"rb"))
   train, val, test                    = x[0], x[1], x[2]
   train_text, val_text, test_text     = x[3], x[4], x[5]
   train_lab, val_lab, test_lab        = x[6], x[7], x[8]
   # wordtoix, ixtoword                  = x[9], x[10]
   if opt.char:
        ## 如果事字符级
        wordtoix, ixtoword, alphabet = x[9], x[10], x[11]
   else:
        wordtoix, ixtoword = x[9], x[10]


   # opt = Options()
   if not opt.char:
        ## 单词级别
        opt.n_words = len(ixtoword) + 1
        ixtoword[opt.n_words-1] = 'GO_' ##加了字符
        
   print dict(opt)
   print('Total words: %d' % opt.n_words)


   run_model(opt, train, val, test, wordtoix, ixtoword)

if __name__ == '__main__':
    main()
