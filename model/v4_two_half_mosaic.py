import os
import traceback

import deeplake as dl
import lovely_tensors as lt
import numpy as np
import torch
import wandb
from composer import Trainer
from composer.loggers import WandBLogger
from modeling_vpt_in_mosaicml import VPT_model  # original work
from termcolor import colored
from tqdm import tqdm

# pyright: reportGeneralTypeIssues=false
# ^^ due to not understanding deeplake
# pyright: reportPrivateImportUsage=false
# pyright: reportOptionalMemberAccess=false
# pyright: reportOptionalCall=false
# ^^ due to not understanding ray

# pip install transformers "deeplake[enterprise]" wandb lovely-tensors  pandas termcolor sentencepiece
# not 100% necessary ofr this file: "ray[default]==2.2.0"
lt.monkey_patch()
# hyperparams
MODEL_VERSION_NAME = 'mosaic_yt_pretrain_half_half'
learning_rate = 1e-4  # also good: 3e-4

BATCH_NAME = "parallel_15"
# BASE_DIR = '/scratch/bbki/kastanday/whisper'
BASE_DIR = '/mnt/storage_ssd'
# BASE_DIR = '~/VPT/'
MODEL_SAVE_PATH = f'{BASE_DIR}/MODEL_CHECKPOINTS/{MODEL_VERSION_NAME}'
# DATABASE_FILEPATH = f'{BASE_DIR}/v4_CLIP_encode_results_{BATCH_NAME}'
DATABASE_FILEPATH = f'{BASE_DIR}/shorter_v4_CLIP_encode_results_{BATCH_NAME}'


def main():
  # create dataloader
  ds = dl.load(DATABASE_FILEPATH)
  columns_for_training = ['clip_pooled_embedding', 'caption_embedding', 'clip_last_hidden_states', 'caption']
  train_dataloader = ds.pytorch(tensors=columns_for_training,
                                transform=my_dataloader_batching_transform,
                                num_workers=0,
                                batch_size=2,
                                pin_memory=False,
                                shuffle=False,
                                drop_last=False)
  eval_dataloader = ds.pytorch(tensors=columns_for_training,
                               transform=my_dataloader_batching_transform,
                               num_workers=0,
                               batch_size=2,
                               pin_memory=False,
                               shuffle=False,
                               drop_last=False)

  # run training with our model
  # todo: implement evaluation or something on a holdout/validation set. Maybe yt1b val.
  model = VPT_model(model_version_name=MODEL_VERSION_NAME)
  optimizer = torch.optim.AdamW(params=model.parameters(),
                                lr=learning_rate)  # Typically, 1e-4 and 3e-4 work well for most problems
  wandb_logger = WandBLogger(
      init_kwargs={
          "config": {
              'learning_rate': learning_rate,
              # 'batch_name': BATCH_NAME,
              'model_save_path': MODEL_SAVE_PATH,
          },
          "entity": "kastan",
          "project": "VPT-custom-t5",
          "name": MODEL_VERSION_NAME,
          # group=datetime_str,
          "tags": [
              'AdamW',
              'MosaicML',
          ],
      })

  trainer = Trainer(
      model=model,
      train_dataloader=train_dataloader,
      optimizers=optimizer,
      max_duration=5,  # epochs 
      device='cpu',  # todo change
      loggers=[wandb_logger],
      seed=42)
  # , # todo
  # eval_dataloader=eval_dataloader,
  # eval_interval='1ep',
  # eval_dataloader=eval_dataloader,
  trainer.fit()


from transformers import T5Tokenizer

device = "cpu"
# device = "cuda" if torch.cuda.is_available() else "cpu"
model_huggingface_name = "google/t5-v1_1-large"
t5_tokenizer = T5Tokenizer.from_pretrained(model_huggingface_name, return_special_tokens_mask=True)


def my_dataloader_batching_transform(segment_batch):
  '''
  param: segment_batch: IterableOrderedDict. 1 SEGMENT (not batch_size, just one).
  We put a bunch of these together to get a btach. 
  
  returns: batch dictionary.  Keys: input_embeds_arr, attn_mask_arr, labels_tokenized
                              Values: batched Torch Tensors of shape <1, 1024, 1024>. These are stacked to create a batch.
  '''
  print("👉👉👉👉👉👉👉👉👉👉👉👉👉👉👉👉👉 SEGMENT BATCH", flush=True)
  batch = {}  # keys: input_embeds_arr, attn_mask_arr, labels

  # Loop over BATCH_SIZE. Create dictionary where key = name, value = batched tensor
  for key, numpy_array in zip(segment_batch.keys(), segment_batch):
    print("------------- PRINTING SEGMENT --------- ")

    if key == 'clip_pooled_embedding':
      print("⭐️1️⃣ pooled embedding")
      # .reshape(batch_size, 1, -1)
      if key in batch.keys():
        batch[key] = torch.cat((batch[key], torch.from_numpy(numpy_array).to(device)), dim=0)
      else:
        batch[key] = torch.from_numpy(numpy_array).to(device)

    elif key == 'caption_embedding':
      print("⭐️2️⃣ caption embedding")
      # keep only the first HALF of caption embedding.
      caption_length = numpy_array.shape[0]
      print("Caption length (should be about 32 ish):", caption_length)
      s_half = caption_length // 2
      # constant length of 446, pad with zeros. 446 is the max length of a caption (1024 - 577 - 1).
      caption_embedding_full_length = torch.zeros((446, 1024)).to(device)
      caption_embedding_full_length[0:s_half] = torch.from_numpy(numpy_array[0:s_half]).to(device)

      # setup attention mask now that we know full length of caption
      att_mask_shape = [1024]
      attn_mask_arr = torch.zeros(att_mask_shape).to(device)
      attn_mask_arr[0:578 + s_half] = 1

      if key in batch.keys():
        # batch[key] = torch.cat((batch[key], torch.ones(10)), dim=0) # todo BAD for testing
        batch[key] = torch.cat((batch[key], caption_embedding_full_length), dim=0)
        batch['attn_mask_arr'] = torch.cat((batch['attn_mask_arr'], attn_mask_arr), dim=0)
      else:
        # batch[key] = torch.ones(10)  # todo BAD for testing
        batch[key] = caption_embedding_full_length
        batch['attn_mask_arr'] = attn_mask_arr

    elif key == 'clip_last_hidden_states':
      print("⭐️3️⃣ clip last hidden states")
      if key in batch.keys():
        batch[key] = torch.cat((batch[key], torch.from_numpy(numpy_array).to(device)), dim=0)
      else:
        batch[key] = torch.from_numpy(numpy_array).to(device)

    elif key == 'caption':
      caption = numpy_array[0]  # passed in as a single-element list.
      print("⭐️4️⃣ CAPTION")
      print(caption)
      full_caption_tokenized = t5_tokenizer(caption, padding=False, truncation=True,
                                            return_tensors="pt").input_ids.to(device)
      print("FULL CAPTION TOKENIZED shape", full_caption_tokenized.shape)
      caption_length = full_caption_tokenized.shape[1]
      s_half = caption_length // 2
      # only keep 2nd half of caption to use as labels.
      print("only 2nd half of captions shape: ", full_caption_tokenized[0][s_half:].shape[0])
      proper_shape = full_caption_tokenized[0][s_half:].shape[0]
      labels_full_length = torch.ones((512), dtype=torch.int64).to(device) * -100
      labels_full_length[:proper_shape] = full_caption_tokenized[0][s_half:]  # 👈 take 2nd half!!
      if 'labels' in batch.keys():
        batch['labels'] = torch.cat((batch['labels'], labels_full_length), dim=0)
      else:
        batch['labels'] = labels_full_length
  return batch


if __name__ == "__main__":
  main()