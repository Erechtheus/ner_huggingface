# -*- coding: utf-8 -*-
"""Kopie von Tokenclassification.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1K-9M3KKeHADEPhk2Oil1GaTxhYq1Dd9W

# Fragen:
 - Woher weiß der Trainer was input und was target ist? Verstehe ich nicht

# Beispiel für Eigennamenerkennung
Das Tutorial wurde stark von [Huggingace](https://huggingface.co/docs/transformers/tasks/token_classification) entlehnt

Was fehlt noch:
 - Dokumente können derzeit länger als TOkengröße sein
 - Zentrale definition des Models
 - Custom trainer funktion
 - W & B + gridsearch
"""

import requests
import itertools
from datasets import Dataset
from datasets import DatasetDict
from transformers import AutoTokenizer
from transformers import DataCollatorForTokenClassification
from transformers import pipeline
import evaluate
import numpy as np
from transformers import AutoModelForTokenClassification, TrainingArguments, Trainer
import time
import pandas as pd
import torch
from seqeval.metrics import classification_report
import wandb

keyFile = open('wandb.key', 'r')
WANDB_API_KEY = keyFile.readline().rstrip()
wandb.login(key=WANDB_API_KEY)

#run = wandb.init(
#    project="HF-NER",
#    notes="NER using HF",
#    tags=["baseline", "bert"],
#)
wandb.init(mode="disabled")

modelCheckpoint = "distilbert-base-uncased"
#modelCheckpoint = "microsoft/BiomedNLP-BiomedBERT-base-uncased-abstract"

device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
print("Device=" +str(device))

f = requests.get("https://raw.githubusercontent.com/Erechtheus/mutationCorpora/master/corpora/IOB/SETH-train.iob")
trainFile = f.text.split("\n")
trainFile.pop(0) #Remove first element

f = requests.get("https://raw.githubusercontent.com/Erechtheus/mutationCorpora/master/corpora/IOB/SETH-test.iob")
testFile = f.text.split("\n")
testFile.pop(0) #Remove first element

def convertToCorpus(inputString):
  documents = []
  document = None
  for line in inputString:
    if line.startswith("#"):
      if document:
        documents.append(document)
      document = {}
      document["id"] = line
      document["tokens"] = []
      document["str_tags"] = []
    else:
      iob = line.rsplit(",",1)
      if len(iob) == 2:
        document["tokens"].append(iob[0])
        document["str_tags"].append(iob[1])
      else:
        print(line)
  return documents

trainCorpus = convertToCorpus(trainFile)
testCorpus = convertToCorpus(testFile)

del(trainFile)
del(testFile)

"""### Übersicht über alle IOB labels"""


label_list = sorted(list(set(list(itertools.chain(*list(map(lambda x : x["str_tags"], trainCorpus)))))))

"""Das ergibt folgende Labelmaps:"""

label2id = dict(zip(label_list, range(len(label_list))))
id2label = {v: k for k, v in label2id.items()}



print(id2label)
print(label2id)

for document in trainCorpus:
  document["ner_tags"] = list(map(lambda x : label2id[x], document["str_tags"]))

for document in testCorpus:
  document["ner_tags"] = list(map(lambda x : label2id[x], document["str_tags"]))


fullData = DatasetDict({
    'train' : Dataset.from_pandas(pd.DataFrame(data=trainCorpus)),
    'test' : Dataset.from_pandas(pd.DataFrame(data=testCorpus))
    })


"""### Trainingsinstanzen:
Für Eigennamenerkennung werden die Daten häufig bereits vortokenisiert und in einem IOB Format verfügbar gemacht:

### Tokenisierung mittels Transformer
Die vorher gezeigte TOkenisierung ist jedoch unzureichend. Wir müssen die tokenisierten Texte noch einmal mittels dem passenden Tokenizer tokenisieren
"""


tokenizer = AutoTokenizer.from_pretrained(modelCheckpoint)

"""Dies wird hier veranschaulicht und die beiden Representationen gegeübergestellt

"""

document = fullData["train"][0]

tokenized_input = tokenizer(document["tokens"], is_split_into_words=True)
labels = document["ner_tags"]

tokens = tokenizer.convert_ids_to_tokens(tokenized_input["input_ids"])
#print("Original-sentence:\t\t" +" ".join(document['tokens']))
#print("Transformer representation:\t" +" ".join(tokens))

def align_labels_with_tokens(labels, word_ids, id2label=id2label, label2id= label2id):
    new_labels = []
    current_word = None
    for word_id in word_ids:
        if word_id != current_word:
            # Start of a new word!
            current_word = word_id
            label = -100 if word_id is None else labels[word_id]
            new_labels.append(label)
        elif word_id is None:
            # Special token
            new_labels.append(-100)
        else:
            # Same word as previous token
            label = labels[word_id]
            # If the label is B-XXX we change it to I-XXX
            if(id2label[label].startswith("B-")):
              label = label2id[id2label[label].replace("B-", "I-")]
#            if label % 2 == 1:
            #    label += 1
            new_labels.append(label)

    return new_labels

word_ids = tokenized_input.word_ids()
#print(labels)
#print(align_labels_with_tokens(labels, word_ids))

## TODO Add here code to show how result would look like

def tokenize_and_align_labels(examples):
    tokenized_inputs = tokenizer(
        examples["tokens"], truncation=True, is_split_into_words=True, max_length=512
    )
    all_labels = examples["ner_tags"]
    new_labels = []
    for i, labels in enumerate(all_labels):
        word_ids = tokenized_inputs.word_ids(i)
        new_labels.append(align_labels_with_tokens(labels, word_ids))

    tokenized_inputs["labels"] = new_labels
    return tokenized_inputs

tokenized_datasets = fullData.map(
    tokenize_and_align_labels,
    batched=True
)

#for token, label in zip(tokenizer.convert_ids_to_tokens(inputs['input_ids'][0]), predicted_token_class):
#    print(token, label)

#for token, label in zip(tokenizer.convert_ids_to_tokens(tokenized_datasets["train"]["input_ids"][0]), tokenized_datasets["train"]["labels"][0]):
#    print(token, id2label[label] if label in id2label else "O")

"""## Tokenisierung
Wie oben gesehen, werden zwei Sondersymbole/Token eingeführt. Der CLS und der SEP Token.  Teilwort-Tokenisierung führt somit zu einer Diskrepanz zwischen der Eingabe und den Labels. Ein einzelnes Wort, das einem einzigen Label entspricht, kann nun in zwei Teilwörter aufgeteilt werden.
Insgesammt führ die Transformer-Tokenisierung zu verschiedenen Problemen:
 - Die beiden Token (CLS/SEP) sollen von unserer Loss-Funktion nicht beachtet werden. Dies erreichen wir, indem wir diesen beiden Token den Label -100 zuweisen. Der Wert -100 wird in der [Cross-Entropy-Loss](https://pytorch.org/docs/stable/generated/torch.nn.CrossEntropyLoss.html) Funktion von Pytorch ignoriert.
 - Zuordnung aller Token zu ihrem entsprechenden Wort mit der Methode [word_ids](https://huggingface.co/docs/transformers/main_classes/tokenizer#transformers.BatchEncoding.word_ids).
 - Kennzeichnung nur des ersten Tokens eines bestimmten Wortes. Die restlichen TOken des selben Wortes erhalten das Ziellabel -100.

Wie in der Vorlesung besprochen müssen in einem Batch alle Inputs die selbe Länge aufweisen. Eine natürliche Grenze ist die Größe des jeweiligen Transformers. Es ist besonders effizient, wenn wir für jeden BAtch die Texte auf die Länge des jeweilig längsten Satzes reduzieren. Dies erfolgt über den DataCollatorForTokenClassification. Alternativ kann man den gesamten Datensatz auf das längste Dokument padden, dies führt aber zu einer längeren Laufzeit.

---
"""


data_collator = DataCollatorForTokenClassification(tokenizer=tokenizer)

"""Um die Leistung des Models während des Trainings zu überwachen sollte man eine Evaluationsfunktion nutzen. Hierfür nutzen wir die altbewährte sequeval Bibliothek. Diese liefert uns Precision, Recall, F1, und Accuracy."""


seqeval = evaluate.load("seqeval")

"""

Wir definieren die Funktion compute_metrics, welche die Vorhersage in "echte" IOB Labels umwandelt und anschließend mittels seqeval die Vorhersage bewertet

"""


#labels = [label_list[i] for i in example[f"ner_tags"]]


def compute_metrics(p):
    predictions, labels = p
    predictions = np.argmax(predictions, axis=2)

    true_predictions = [
        [label_list[p] for (p, l) in zip(prediction, label) if l != -100]
        for prediction, label in zip(predictions, labels)
    ]
    true_labels = [
        [label_list[l] for (p, l) in zip(prediction, label) if l != -100]
        for prediction, label in zip(predictions, labels)
    ]

    results = seqeval.compute(predictions=true_predictions, references=true_labels)
    return {
        "precision": results["overall_precision"],
        "recall": results["overall_recall"],
        "f1": results["overall_f1"],
        "accuracy": results["overall_accuracy"],
    }

"""# Training"""


model = AutoModelForTokenClassification.from_pretrained(
    modelCheckpoint,
    num_labels=len(id2label),
    id2label=id2label,
    label2id=label2id
)
model.to(device)

training_args = TrainingArguments(
    output_dir="my_awesome_wnut_model",
    learning_rate=2e-5,
    per_device_train_batch_size=4,
    per_device_eval_batch_size=4,
    num_train_epochs=5,
    weight_decay=0.01,
    save_total_limit = 3,
    evaluation_strategy="epoch",
    save_strategy="epoch",
    load_best_model_at_end=True,
    push_to_hub=False,

    # other args and kwargs here
    report_to="wandb",  # enable logging to W&B
    run_name="bert-ner",  # name of the W&B run (optional)
    logging_steps=1,  # how often to log to W&B
)

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=tokenized_datasets["train"],
    eval_dataset=tokenized_datasets["test"],
    tokenizer=tokenizer,
    data_collator=data_collator,
    compute_metrics=compute_metrics,
)


start = time.time()
trainer.train()
#Plot
"""
print("Finished after " +str(datetime.timedelta(seconds=round(time.time() - start))))

pd.DataFrame(trainer.state.log_history).head(5)

df = pd.DataFrame(trainer.state.log_history)
df = df[df.eval_runtime.notnull()]
df.plot(x='epoch', y=['eval_loss'], kind='bar')

df.plot(x='epoch', y=['eval_precision', 'eval_recall', 'eval_f1'], kind='bar', figsize=(15,9))
"""
"""# Inferenz
Wir wollen für den unten genannten Text die Entitäten vorhersagen.
"""

"""
text = "Identification of four novel mutations in the factor VIII gene: three missense mutations (E1875G, G2088S, I2185T) and a 2-bp deletion (1780delTC)."

inputs = tokenizer(text, return_tensors="pt")
print(inputs)
"""

"""Input und Model müssen im selben RAM (hier GPU) liegen."""
"""
inputs.to(device)

with torch.no_grad():
  logits = model(**inputs).logits
"""
"""Über logits ermitteln wir  die Klasse mit der höchsten Wahrscheinlichkeit und verwenden die id2label-Zuordnung des Modells, um sie in eine Textbezeichnung umzuwandeln."""
"""
print(logits)

predictions = torch.argmax(logits, dim=2)
predicted_token_class = [model.config.id2label[t.item()] for t in predictions[0]]

for token, label in zip(tokenizer.convert_ids_to_tokens(inputs['input_ids'][0]), predicted_token_class):
    print(token, label)
"""
"""# Alternatively use pipeline"""
"""
clf = pipeline("token-classification", model, tokenizer=tokenizer, device=device)
answer = clf(text)
print(answer)
"""
"""# Final eval"""

predictions = trainer.predict(tokenized_datasets["test"])


for testDoc in tokenized_datasets["test"]:
  with torch.no_grad():
    logits = model(**testDoc).logits

pred = []
for line in np.argmax(predictions.predictions, axis=2):
  pred.append([id2label[a] for a in line if a != 0])

print(len(pred[1]))

gold = []
for line in tokenized_datasets["test"]["labels"]:
  #print([id2label[a] for a in line if a != -100] )
  gold.append([id2label[a] for a in line if a != -100 ])

np.argmax(predictions.predictions, axis=2)[0]

tokenized_datasets["test"]["labels"][0]

print(classification_report(gold, pred))