"""
Preprocess a raw json dataset into hdf5/json files for use in data_loader.lua

Input: json file that has the form
[{ file_path: 'path/img.jpg', captions: ['a caption', ...] }, ...]
example element in this list would look like
{'captions': [u'A man with a red helmet on a small moped on a dirt road. ', u'Man riding a motor bike on a dirt road on the countryside.', u'A man riding on the back of a motorcycle.', u'A dirt path with a young person on a motor bike rests to the foreground of a verdant area with a bridge and a background of cloud-wreathed mountains. ', u'A man in a red shirt and a red hat is on a motorcycle on a hill side.'], 'file_path': u'val2014/COCO_val2014_000000391895.jpg', 'id': 391895}

This script reads this json, does some basic preprocessing on the captions
(e.g. lowercase, etc.), creates a special UNK token, and encodes everything to arrays

Output: a json file and an hdf5 file
The hdf5 file contains several fields:
/images is (N,3,256,256) uint8 array of raw image data in RGB format， N是总共image的数目
/labels is (M,max_length) uint32 array of encoded labels, zero padded
/label_start_ix and /label_end_ix are (N,) uint32 arrays of pointers to the 
  first and last indices (in range 1..M) of labels for each image，描述一副图像所用的captions的范围位置
/label_length stores the length of the sequence for each of the M sequences

The json file has a dict that contains:
- an 'ix_to_word' field storing the vocab in form {ix:'word'}, where ix is 1-indexed
- an 'images' field that is a list holding auxiliary information for each image, 
  such as in particular the 'split' it was assigned to.
"""

import os
import json
import argparse
from random import shuffle, seed
import string
# non-standard dependencies:
import h5py
import numpy as np
from scipy.misc import imread, imresize

def prepro_captions(imgs):
  
  # preprocess all the captions
  print 'example processed tokens:'
  for i,img in enumerate(imgs):
    # 为每一个img建立一个新的field， 该field包含预处理后tokens
    img['processed_tokens'] = []
    for j,s in enumerate(img['captions']):
      #translate：对于string对象， 第一个参数设为None， 就是删除string中出现的后面的字符, 这里是删掉标点符号
      #strip([chars])：Returns a copy of the string with the leading and trailing characters removed，
      #                if empty arguments, remove space， 这里是删掉最前和最后出现的空格
      # split([sep])：Returns a list of the words in the string, separated by the delimiter string. 
      #               参数为空， 则默认为空格切分， 注意返回的是一个list
      txt = str(s).lower().translate(None, string.punctuation).strip().split()
      img['processed_tokens'].append(txt)
      # 打印前10个图像， 每个图像对应的第一个caption的处理后的切分结果
      if i < 10 and j == 0: print txt

# 构建caption的相关字典
def build_vocab(imgs, params):
  # 统计构建字典的单词频率阈值
  count_thr = params['word_count_threshold']

  # count up the number of words
  counts = {}  # dict
  for img in imgs:
    for txt in img['processed_tokens']:
      for w in txt:
        # 统计出现个数， w是字典counts的一个key
        counts[w] = counts.get(w, 0) + 1
  # 列表推导， 由counts字典得到新的字典， 此时键变成了count， 并
  # 按照count进行从大到小排序
  cw = sorted([(count,w) for w,count in counts.iteritems()], reverse=True)
  print 'top words and their counts:'
  #　str函数： Returns a string containing a printable representation of an object
  print '\n'.join(map(str,cw[:20]))

  # print some stats
  # 所有word的个数
  # dict.itervalues(): Returns an iterator over the dictionary’s values.
  total_words = sum(counts.itervalues())
  print 'total words:', total_words
  # 将会被视为UNK字符
  bad_words = [w for w,n in counts.iteritems() if n <= count_thr]
  # 最终的字典，注：字典的每一个字符出现的次数大于一个阈值
  vocab = [w for w,n in counts.iteritems() if n > count_thr]
  # UNK字符出现的次数
  bad_count = sum(counts[w] for w in bad_words)
  print 'number of bad words: %d/%d = %.2f%%' % (len(bad_words), len(counts), len(bad_words)*100.0/len(counts))
  print 'number of words in vocab would be %d' % (len(vocab), )
  print 'number of UNKs: %d/%d = %.2f%%' % (bad_count, total_words, bad_count*100.0/total_words)

  # lets look at the distribution of lengths as well
  sent_lengths = {} # dict which stores element like (nw, count)
  for img in imgs:
    for txt in img['processed_tokens']:
      nw = len(txt)
      sent_lengths[nw] = sent_lengths.get(nw, 0) + 1
  max_len = max(sent_lengths.keys())
  print 'max length sentence in raw data: ', max_len
  print 'sentence length distribution (count, number of words):'
  sum_len = sum(sent_lengths.values())
  for i in xrange(max_len+1):
    print '%2d: %10d   %f%%' % (i, sent_lengths.get(i,0), sent_lengths.get(i,0)*100.0/sum_len)

  # lets now produce the final annotations
  # 将UNK插入到字典中
  if bad_count > 0:
    # additional special UNK token we will use below to map infrequent words to
    print 'inserting the special UNK token'
    vocab.append('UNK')
  
  # 进一步处理图像的caption，生成每个图像最终的caption
  for img in imgs:
    # 创建一个新的field， 存放最终的caption
    img['final_captions'] = []  # list
    for txt in img['processed_tokens']:
      # 将caption中单词小于一定阈值的单词替换成UNK token
      caption = [w if counts.get(w,0) > count_thr else 'UNK' for w in txt]
      img['final_captions'].append(caption)

  return vocab
  
# 划分训练集， 分配出训练集， 测试集， 验证集
def assign_splits(imgs, params):
  num_val = params['num_val']
  num_test = params['num_test']

  for i,img in enumerate(imgs):
      if i < num_val:
        img['split'] = 'val'
      elif i < num_val + num_test: 
        img['split'] = 'test'
      else: 
        img['split'] = 'train'

  print 'assigned %d to val, %d to test.' % (num_val, num_test)

def encode_captions(imgs, params, wtoi):
  """ 
  encode all captions into one large array, which will be 1-indexed.
  also produces label_start_ix and label_end_ix which store 1-indexed 
  and inclusive (Lua-style) pointers to the first and last caption for
  each image in the dataset.
  """

  max_length = params['max_length']
  N = len(imgs)  # 图像的总数目
  M = sum(len(img['final_captions']) for img in imgs) # total number of captions， 约为N的5倍

  label_arrays = []
  label_start_ix = np.zeros(N, dtype='uint32') # note: these will be one-indexed
  label_end_ix = np.zeros(N, dtype='uint32')
  
  label_length = np.zeros(M, dtype='uint32') 
  
  caption_counter = 0
  counter = 1   
  for i,img in enumerate(imgs):
    n = len(img['final_captions'])  # img具有的captions的个数
    assert n > 0, 'error: some image has no captions'

    Li = np.zeros((n, max_length), dtype='uint32')
    for j,s in enumerate(img['final_captions']):
      # label_length[0], label_length[1], ...
      label_length[caption_counter] = min(max_length, len(s)) # record the length of this sequence
      caption_counter += 1
      for k,w in enumerate(s):
        # 确保Li长度小于规定的最大的长度，超过部分即切断
        if k < max_length:
          Li[j,k] = wtoi[w]

    # note: word indices are 1-indexed, and captions are padded with zeros
    label_arrays.append(Li)
    # 比如， counter = 1， 1+5-1， 更新counter为6
    label_start_ix[i] = counter
    label_end_ix[i] = counter + n - 1
    
    counter += n
  
  L = np.concatenate(label_arrays, axis=0) # put all the labels together， along dimension 0 
  assert L.shape[0] == M, 'lengths don\'t match? that\'s weird'
  # 判断label_length 
  assert np.all(label_length > 0), 'error: some caption had no words?'

  print 'encoded captions to array of size ', `L.shape`
  return L, label_start_ix, label_end_ix, label_length

def main(params):

  imgs = json.load(open(params['input_json'], 'r'))
  seed(123) # make reproducible
  shuffle(imgs) # shuffle the order

  # tokenization and preprocessing
  prepro_captions(imgs)

  # create the vocab
  vocab = build_vocab(imgs, params)
  itow = {i+1:w for i,w in enumerate(vocab)} # a 1-indexed vocab translation table
  wtoi = {w:i+1 for i,w in enumerate(vocab)} # inverse table

  # assign the splits
  assign_splits(imgs, params)
  
  # encode captions in large arrays, ready to ship to hdf5 file
  L, label_start_ix, label_end_ix, label_length = encode_captions(imgs, params, wtoi)

  # create output h5 file
  N = len(imgs)
  f = h5py.File(params['output_h5'], "w")
  f.create_dataset("labels", dtype='uint32', data=L)
  f.create_dataset("label_start_ix", dtype='uint32', data=label_start_ix)
  f.create_dataset("label_end_ix", dtype='uint32', data=label_end_ix)
  f.create_dataset("label_length", dtype='uint32', data=label_length)
  dset = f.create_dataset("images", (N,3,256,256), dtype='uint8') # space for resized images
  for i,img in enumerate(imgs):
    # load the image
    I = imread(os.path.join(params['images_root'], img['file_path']))
    try:
        Ir = imresize(I, (256,256))
    except:
        print 'failed resizing image %s - see http://git.io/vBIE0' % (img['file_path'],)
        raise
    # handle grayscale input images
    if len(Ir.shape) == 2:
      Ir = Ir[:,:,np.newaxis]
      Ir = np.concatenate((Ir,Ir,Ir), axis=2)
    # and swap order of axes from (256,256,3) to (3,256,256)
    Ir = Ir.transpose(2,0,1)
    # write to h5
    dset[i] = Ir
    if i % 1000 == 0:
      print 'processing %d/%d (%.2f%% done)' % (i, N, i*100.0/N)
  f.close()
  print 'wrote ', params['output_h5']

  # create output json file
  out = {}  # dict 
  out['ix_to_word'] = itow # encode the (1-indexed) vocab
  out['images'] = []
  for i,img in enumerate(imgs):
    
    jimg = {}  # dict 
    jimg['split'] = img['split']
    if 'file_path' in img: jimg['file_path'] = img['file_path'] # copy it over, might need
    if 'id' in img: jimg['id'] = img['id'] # copy over & mantain an id, if present (e.g. coco ids, useful)
    
    out['images'].append(jimg)
  # dump dict out to file params['output_json']
  json.dump(out, open(params['output_json'], 'w'))
  print 'wrote ', params['output_json']


if __name__ == "__main__":

  parser = argparse.ArgumentParser()

  # input json
  parser.add_argument('--input_json', required=True, help='input json file to process into hdf5')
  parser.add_argument('--num_val', required=True, type=int, help='number of images to assign to validation data (for CV etc)')
  parser.add_argument('--output_json', default='data.json', help='output json file')
  parser.add_argument('--output_h5', default='data.h5', help='output h5 file')
  
  # options
  parser.add_argument('--max_length', default=16, type=int, help='max length of a caption, in number of words. captions longer than this get clipped.')
  parser.add_argument('--images_root', default='', help='root location in which images are stored, to be prepended to file_path in input json')
  parser.add_argument('--word_count_threshold', default=5, type=int, help='only words that occur more than this number of times will be put in vocab')
  parser.add_argument('--num_test', default=0, type=int, help='number of test images (to withold until very very end)')

  args = parser.parse_args()
  params = vars(args) # convert to ordinary dict
  print 'parsed input parameters:'
  print json.dumps(params, indent = 2)
  main(params)
