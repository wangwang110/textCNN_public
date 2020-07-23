# -*- coding: utf-8 -*-
import cPickle
import pdb
import numpy as np
import os


def idx2sent(text, wordtoix):
    #char_x=idx2sent(x, alphabet)
    char_seq = []
    for it in text:
        it_list = list(it)
        text_int8_repr=[]
        for x in it_list:
            text_int8_repr.append(wordtoix[x])
        char_seq.append(text_int8_repr)
    return char_seq


def prepare_data_for_charCNN(loadpath = "./data/yahoo_char.p"):

    x = cPickle.load(open(loadpath,"rb"))

#    train, val, test                    = x[0], x[1], x[2]
#    train_text, val_text, test_text     = x[3], x[4], x[5]
#    train_lab, val_lab, test_lab        = x[6], x[7], x[8]
    wordtoix, ixtoword, alphabet = x[9], x[10], x[11]
 
    pairs=[]
    path = "./newdata/" #文件夹目录
    files= os.listdir(path) 
    for infile in files:
         c=open(path+infile,'r')
         lines=c.readlines()
         length=len(lines)
         for i in range(0,length,3):
#             print lines[i]
#             print lines[i+1]
             pairs.append((lines[i].strip(),lines[i+1].strip()))
    
    shuffle_indices=np.random.permutation(np.arange(len(pairs)))
    pairs=np.array(pairs)[shuffle_indices]
    
    for x,y in pairs:
        char_x=idx2sent(x, wordtoix)
        char_y=idx2sent(y, wordtoix)

    with open('./data/collect_data', 'w+') as f:
        cPickle.dump([char_x, char_y], f)

if __name__ == '__main__':

    prepare_data_for_charCNN()
