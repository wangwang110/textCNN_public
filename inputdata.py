# -*- coding: utf-8 -*-
"""
Created on Fri Jul 20 09:10:54 2018

@author: cvter
"""
 
import pickle
loadpath = './data/yahoo_char.p'
x = pickle.load(open(loadpath,"rb"))
train, val, test                    = x[0], x[1], x[2]
train_text, val_text, test_text     = x[3], x[4], x[5]
train_lab, val_lab, test_lab        = x[6], x[7], x[8]
# wordtoix, ixtoword                  = x[9], x[10]

## 如果事字符级别
wordtoix, ixtoword, alphabet = x[9], x[10], x[11]
