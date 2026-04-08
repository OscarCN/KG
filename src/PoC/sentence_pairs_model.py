import os
import sys
import json
import time
import pickle
import random

import pandas as pn
import numpy as np

import tensorflow_text  # Do not remove, sometimes the universal sentence encoder doesn't work if not imported
import tensorflow_hub as hub

from datetime import datetime, timedelta
from dotenv import load_dotenv
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor, as_completed

from sklearn.neighbors import KDTree
from utils.connections import es_query, es_topics_query

from collections import defaultdict
from sklearn.neighbors import NearestNeighbors


use = hub.load("https://tfhub.dev/google/universal-sentence-encoder-multilingual-large/3")

########################################################################################################################
# BUILD TRAINING SET

build_training_set = False

if build_training_set:
    since = datetime(2023, 9, 16)
    until = since + timedelta(hours=2)
    topics = []
    while since < datetime(2024, 8, 23):
        _, c_topics = es_topics_query(since=since, until=until, max_hits=250, index='topics', date_field='topic_date', project=['topic_id', 'topic_name', 'summary', 'custom_categories', 'embedding_v0', 'topic_date', 'related_documents'])
        print(since, len(topics), len(c_topics))
        topics += c_topics
        since += timedelta(days=1, hours=7)
        until += timedelta(days=1, hours=7)

    p = np.array([t['related_documents']['news_count'] for t in topics])
    p = np.power(p, 2) / np.power(p, 2).sum()
    idx_sample = np.random.choice(range(len(topics)), size=1000, p=p)  # Sample more from bigger events (topics)

    all_pairs = list()
    for ii, idx in enumerate(idx_sample):
        if ii % 100 == 0:
            print(ii)
        topic_id = topics[idx]['topic_id']
        _, _docs = es_query(event_id=topic_id)

        seen = set()
        docs = []
        art_embs = []
        for d in _docs:
            if d['title'] not in seen and 'embedding_v0' in d:
                docs.append(d)
                art_embs.append(np.array(d['embedding_v0']))
                seen.add(d['title'])

        art_embs = np.array(art_embs)

        # REMOVE (ALMOST) DUPLICATE SENTENCES ##########
        n_ch_dups = min(int(art_embs.shape[0] / 2), 75)
        nn = NearestNeighbors(n_neighbors=n_ch_dups, metric='cosine')
        nn.fit(art_embs)

        dists, indices = nn.kneighbors(art_embs)

        dupsmap = defaultdict(set)
        dupsmap_url = defaultdict(set)
        seen = set()
        for i in range(len(docs)):
            for k in range(n_ch_dups):
                if dists[i, k] > .1:
                    break
                elif (indices[i, k] not in seen) and i != indices[i, k]:
                    dupsmap[i].add(indices[i, k])
                    dupsmap_url[docs[i]['url']].add(docs[indices[i, k]]['url'])
                    seen.add(indices[i, k])
                    seen.add(i)

        dups = {t for k in dupsmap.values() for t in k}

        docs = [t for i, t in enumerate(docs) if i not in dups]

        ####################

        sentences = []
        dates = []
        idxs = []

        for i, d in enumerate(docs):
            c_sents = d['text'].split('\n')
            sentences += c_sents
            idxs += [i] * len(c_sents)
            dates += [d['date_created']] * len(c_sents)

        df_sents = pn.DataFrame({'sentence': sentences, 'article': idxs, 'date': dates})

        # Sentences shorter than 150 characters are usually noise
        df_sents = df_sents[df_sents.sentence.map(lambda t: len(t) >= 150)]
        df_sents.index = range(len(df_sents))

        embs = None
        batch_size = 128
        i = 0
        while i * batch_size < len(df_sents):
            if embs is None:
                embs = use(df_sents.sentence.iloc[i * batch_size:(i + 1) * batch_size].tolist())
            else:
                embs = np.concatenate((embs, use(df_sents.sentence.iloc[i*batch_size:(i+1)*batch_size].tolist())))
            i += 1

        nn_tree = KDTree(embs)

        n_sents = min(max(int(len(df_sents) * .05), 5), int(len(df_sents)-2))
        idx_sents_sample = random.sample(range(len(df_sents)), n_sents)
        pairs = list()
        for i in idx_sents_sample:
            #nn_tree.query(embs[i:i+1, :])
            pairs += [{'text 1': df_sents.sentence.iloc[i], 'text 2': t, 'topic_id': topic_id} for t in random.sample(df_sents.iloc[nn_tree.query(embs[i:i+1, :], k=n_sents)[1][0][1:]].sentence.tolist(), min(n_sents-2, 5))]

        all_pairs += pairs

    df_pairs = pn.DataFrame(all_pairs)
    df_pairs['idx'] = range(len(all_pairs))

    df_pairs.to_csv('/Users/oscarcuellar/ocn/media/kg/spanish_EL/scripts/data/pairs_train_raw.csv', index=False)

########################################################################################################################
# LLM TAG TRAINING SET

tag_llm = False
if tag_llm:

    sys_1 = 'Eres un modelo para clasificar si dos textos se refieren a lo mismo, se te van a presentar instrucciones para resolver la tarea, algunos ejemplos, y posteriormente textos para clasificar'

    usr_1 = '''
    Decimos que dos textos hablan de lo mismo si relatan el mismo hecho o contienen la misma información, incluso cuando un texto contiene más información que el otro'''
    usr_2 = '''Se te van a presentar extractos de noticias, cada par de textos proviene de artículos que se refieren al mismo evento, sin embargo pueden no contener la misma información.
    por ejemplo, el texto 
    "El Sindicato de Trabajadores del PJF pidió a sus agremiados no participar en paros de labores que no han sido decretados de forma oficial a fin de no dificultar la eventual defensa contra la parte patronal." contiene la misma información que "Gilberto González Pimentel, líder del Sindicato de Trabajadores del Poder Judicial de la Federación (STPJF), ha solicitado a sus miembros que no apoyen la suspensión de labores. Argumenta que el paro podría complicar la defensa legal de los trabajadores en caso de sanciones."
    por otro lado, el texto "El Sindicato de Trabajadores del PJF pidió a sus agremiados no participar en paros de labores que no han sido decretados de forma oficial a fin de no dificultar la eventual defensa contra la parte patronal." no contiene la misma información que "El pasado 13 y 14 de agosto, las ocho secciones del Sindicato de Trabajadores del PJF decidieron participar en una suspensión de labores a la espera del dictamen de la Comisión de Puntos Constitucionales de la Cámara de Diputados, que debía garantizar la protección de sus derechos laborales y prestaciones. No obstante, con la publicación del nuevo dictamen, un grupo de trabajadores decidió iniciar el paro de inmediato.", ya que, aunque ambos hacen referencia al mismo paro de labores, el primero habla del sindicato pidiendo que no inicien por su cuenta, mientras que el segundo habla de el dictamen correspondiente y de cómo algunos iniciaron el paro"
    
    si dos textos contienen la misma información deberás responder con 1, si no contienen la misma información deberás responder con 0
    
    ejemplos:
    
    texto 1: "Los principales puntos de preocupación de los trabajadores judiciales radican en que la propuesta de reforma elimina la carrera judicial como sistema de mérito, un pilar que consideran esencial para garantizar la imparcialidad y calidad en la impartición de justicia. La reforma también incluye la elección por voto popular de ministros, magistrados y jueces."
    texto 2: "La Reforma Judicial plantea transformar el sistema de justicia y que los jueces, magistrados y ministros de la Suprema Corte de Justicia de la Nación sean elegidos por voto popular"
    respuesta: 1
    
    texto 1: "El resultado se dará a conocer en el transcurso de la noche de este lunes y el miércoles se iniciaría o no el paro de labores de jueces y magistrados."
    texto 2: "Esta medida fue organizada por Jufed, que la semana pasada lanzó la convocatoria y diseñó la plataforma para permitir la participación de juezas, jueces, magistradas, magistrados, así como asociados y jubilados. El paro oficial se iniciará el miércoles próximo"
    respuesta: 1
    
    texto 1: "Rechazó que sea la ministra presidenta de la Suprema Corte de Justicia de la Nación (SCJN), Norma Piña, manipule a los trabajadores para que realicen el paro."
    texto 2: "La ministra Yasmín Esquivel Mossa y la ministra Loretta Ortiz, cuestionaron abiertamente a la presidenta de la Corte, Norma Piña, sobre su posicionamiento contra la reforma impulsada el partido de Morena"
    respuesta: 0
    
    texto 1: "Trabajadores del Poder Judicial iniciaron el paro indefinido de labores para mostrar su inconformidad ante la reforma propuesta por el presidente Andrés Manuel López Obrador, el pasado 5 de febrero y con la que se busca que el nombramiento y selección de los ministros de la Suprema Corte de Justicia de la Nación (SCJN), así como los magistrados y jueces sean elegidos por la ciudadanía"
    texto 2: "Este lunes, trabajadores del Poder Judicial iniciaron un paro indefinido de labores y acusaron que los foros públicos organizados por la Cámara de Diputados fueron una farsa y que la reforma al Poder Judicial que impulsa el presidente López Obrador es regresiva y violenta los derechos de los ciudadanos."
    respuesta: 1
    
    texto 1: "La decisión de suspender actividades en el Poder Judicial es motivada por la reforma constitucional que plantea Morena y aliados. Esta propone la elección de ministros, jueces y magistrados a través del voto popular, así como otras medidas que podrían afectar los derechos laborales de trabajadores debido a la eliminación de fideicomisos"
    texto 2: "Los principales puntos de preocupación de los trabajadores judiciales radican en que la propuesta de reforma elimina la carrera judicial como sistema de mérito, un pilar que consideran esencial para garantizar la imparcialidad y calidad en la impartición de justicia. La reforma también incluye la elección por voto popular de ministros, magistrados y jueces"
    respuesta: 1
    
    texto 1: "Esta suspensión de labores está respaldada por algunos jueces y magistrados; sin embargo, la Asociación Nacional de Magistrados de Circuito y Jueces de Distrito del Poder Judicial de la Federación (JUFED) convocó a sus integrantes a votar para la suspensión temporal de las actividades jurisdiccionales en protesta por la Reforma Judicial"
    texto 2: "Los trabajadores del Poder Judicial comenzaron este lunes una huelga indefinida contra la reforma que busca garantizar la elección por voto de jueces, magistrados y ministros, no obstante, el Instituto Federal de la Defensoría Pública Federal (IFDP) seguirá atendiendo de manera presencial a las personas que lo requieran, así como sus delegaciones y por medio de Defensatel"
    respuesta: 0
    
    texto 1: "La morenista adelantó que el próximo martes se reunirá con López Obrador, cuyo mandato termina el 30 de septiembre, para discutir el presupuesto del Gobierno, tal como adelantó el mandatario saliente en su conferencia matutina"
    texto 2: "Desde su casa de transición, la morenista dijo que el 26 de agosto hará público el nombre de quien dirigirá la política de Pemex durante los próximos 6 años"
    respuesta: 0
    
    texto 1: "Durante un foro de negocios, el embajador de Canadá en México, Graeme C. Clark, informó que empresarios canadienses le han expresado su preocupación por la iniciativa que plantea la elección de jueces por el voto popular"
    texto 2: "Graeme C. Clark, embajador de Canadá en México, expresó algunas inquietudes por la reforma al Poder Judicial que impulsa el presidente Andrés Manuel López Obrador y por la que los trabajadores mantienen un paro de labores"
    respuesta: 1
    
    texto 1: "Durante un foro de negocios, el embajador de Canadá en México, Graeme C. Clark, informó que empresarios canadienses le han expresado su preocupación por la iniciativa que plantea la elección de jueces por el voto popular"
    texto 2: "Como diplomático, soy muy sensible a cualquier comentario que podría ser visto como una injerencia en los asuntos de México y ciertamente no es el propósito”, puntualizó Clark, quien también dijo que la reforma judicial era un tema que desde la Embajada canadiense habían estado siguiendo “con mucho interés"
    respuesta: 0
    
    texto 1: "Por regla de paridad, se aprobó no entregar el lugar al dirigente de Movimiento Ciudadano, Dante Delgado, y cedérsela a Amalia García, porque este partido era el que menos representación daba a las mujeres.“Movimiento Ciudadano es el que tiene mayor subrrrepresentación de mujeres, es un 20 por ciento mientras que Morena tiene 48.33 por ciento, el acuerdo previo establecía que el partido con menor representación recibiría el ajuste y ése es Movimiento Ciudadano”, expuso la consejera Dania Ravel."
    texto 2: "En representación de Movimiento Ciudadano, Braulio López expresó su rechazo al ajuste por paridad de género, que dejaría fuera a su dirigente, Dante Delgado, ya que esto no aplicaría según el acuerdo del INE, y en su lugar, pidió aplicar esta modificación a Morena."
    respuesta: 1
    '''

    usr_3_ = '''
    A continuación se te presentan 2 textos para clasifiques si mencionan la misma información:
    texto 1: "{t1}"
    texto 2: "{t2}"
    '''

    #tagged = dict()

    OPENAI_PROJECT_ID = os.getenv('OPENAI_PROJECT_ID', 'news_KG')
    OPENAI_KEY = os.getenv('OPENAI_API_KEY')
    OPENAI_ORGANIZATION = os.getenv('OPENAI_ORGANIZATION', 'org-OSGYrp5SnEAis7CDgoxEmNiu')
    OPENAI_MODEL = 'gpt-4o'

    client = OpenAI(
        api_key=OPENAI_KEY,
        organization=OPENAI_ORGANIZATION,
    )

    #training_data = pn.read_csv('/Users/oscarcuellar/ocn/media/kg/spanish_EL/scripts/data/pairs_train_raw.csv')
    training_data = pn.read_csv('/Users/oscarcuellar/ocn/media/kg/spanish_EL/scripts/data/pairs_train_tagged_part.csv')
    tagged = dict(training_data[~pn.isnull(training_data.y)].idx, training_data[~pn.isnull(training_data.y)].y)

    def tag(_id, t1, t2):
        if _id not in tagged:

            usr_3 = usr_3_.format(t1=t1, t2=t2)

            messages = [
                {"role": "system", "content": sys_1},
                {"role": "user", "content": usr_1},
                {"role": "user", "content": usr_2},
                {"role": "user", "content": usr_3},
            ]
            retries = 0
            retry = True
            while retry:
                try:
                    response = client.chat.completions.create(
                        model=OPENAI_MODEL,
                        messages=messages,
                        temperature=0.7,
                    )
                    retry = False
                except Exception as ex:
                    print('sleeping', ex)
                    time.sleep(15)
                    retries += 1
                    if retries > 4:
                        retry = False

            try:
                tagged[_id] = json.loads(response.choices[0].message.content.replace('`', '').replace('json', ''))
            except:
                print('exception parsing', _id)
                tagged[_id] = response


    articles = training_data.iloc[np.random.permutation(range(len(training_data)))].to_dict(orient='records')

    for i, art in enumerate(articles[:10000]):
        if i % 10 == 0:
            print(i)
        tag(art['idx'], art['text 1'], art['text 2'])


    import re

    for k, v in tagged.items():
        if not isinstance(v, int):
            tagged[k] = int(re.findall('\d', v.choices[0].message.content)[0])
            print(tagged[k])

    training_data.to_csv('/Users/oscarcuellar/ocn/media/kg/spanish_EL/scripts/data/pairs_train_tagged_part.csv', index=False)


########################################################################################################################
# UNIVERSAL SENTENCE ENCODER FEATURES (DOESN'T WORK WELL)

data = pn.read_excel('sentence_pairs_model.xlsx')
data = data[~pn.isnull(data.y)]

sents = set(data['text 1'].tolist() + data['text 2'].tolist())
sent_embs = {t: np.nan for t in sents}
sents = list(sent_embs.keys())

batch_size = 256
i = 0
while i * batch_size < len(sents):
    if i % 10 == 0:
        print(i * batch_size)

    process_sents = [sents[t] for t in range(i * batch_size, min((i + 1) * batch_size, len(sents)))]

    embs = use(process_sents)

    for k, t in enumerate(process_sents):
        sent_embs[t] = embs[k]

    i += 1

for k in sent_embs.keys():
    sent_embs[k] = sent_embs[k].numpy()

features = list()
for i, r in data.iterrows():
    features.append(np.concatenate((sent_embs[r['text 1']], sent_embs[r['text 2']]), axis=0))

use_features = np.array(features)


########################################################################################################################
# OAI_FEATURES

use_oai_features = False
if use_oai_features:
    oai_sent_embs = dict()
    oai_sent_embs_large = dict()

    def get_embedding_large(text, idx, model='text-embedding-3-large'):
        text = text.replace("\n", " ")
        e = client.embeddings.create(input=[text], model=model).data[0].embedding
        oai_sent_embs_large[text] = np.array(e)
        return idx, np.array(e)

    def get_embedding(text, idx, model="text-embedding-3-small"):
        text = text.replace("\n", " ")
        e = client.embeddings.create(input=[text], model=model).data[0].embedding
        oai_sent_embs[text] = np.array(e)
        return idx, np.array(e)

    def openai_embeddings(sents, large=False):

        sents = [(i, t) for i, t in enumerate(sents)]
        batches = [sents[i:i + 10] for i in range(0, len(sents), 10)]

        func = get_embedding_large if large else get_embedding
        # process each batch in parallel
        ret_sents = list()
        for i, batch in enumerate(batches):

            with ThreadPoolExecutor() as executor:
                futures = [executor.submit(func, txt, idx) for idx, txt in batch]

                for future in as_completed(futures):
                    try:
                        result = future.result()

                        ret_sents.append(result)

                    except Exception as e:
                        print(f"Error occurred in sub-thread: {e}")
            if i % 100 == 0:
                print(f"Processed {i + 1}/{len(batches)} batches")
        return np.array([t[1] for t in sorted(ret_sents, key=lambda t: t[0])])


    oai_ = openai_embeddings(sents)
    oai_large = openai_embeddings(sents, large=True)

    features = list()
    for i, r in data.iterrows():
        features.append(np.concatenate((oai_sent_embs_large[r['text 1']], oai_sent_embs_large[r['text 2']]), axis=0))

    oai_features = np.array(features)

    features = oai_features.astype('float32')

########################################################################################################################
# TODO: Finetune Nomic embeddings model with deep river news
# TODO: Plug Nomic embeddings instead
# TODO: Finetune Nomic model with this downstream task


#
########################################################################################################################
# Sentence Pairs Model

Y = data.y.values.astype('int64')

N = len(features)
ntrain = int(.85 * N)
nvalid = int(.1 * N)
ntest = N - (ntrain + nvalid)

disorder = random.sample(range(N), N)

features = features[disorder]
Y = Y[disorder]
weights = Y + 1

train_x = features[:ntrain]
train_y = Y[:ntrain]
train_weights = weights[:ntrain]

valid_x = features[ntrain:ntrain + nvalid]
valid_y = Y[ntrain:ntrain + nvalid]
valid_weights = weights[ntrain:ntrain + nvalid]

test_x = features[-ntest:]
test_y = Y[-ntest:]
test_weights = weights[-ntest:]

train_x_bis = train_x.copy()
ph = train_x_bis[:, :512]
train_x_bis[:, :512] = train_x_bis[:, 512:1024]
train_x_bis[:, 512:1024] = ph

train_x = np.concatenate((train_x, train_x_bis))
train_y = np.concatenate((train_y, train_y))
train_weights = np.concatenate((train_weights, train_weights))

########################################################################################################################
# MODEL ################################################################################################################
########################################################################################################################

import torch
import torch.nn as nn

class RESNet(torch.nn.Module):

    def __init__(self, dim, r_dropout=.5):
        super(RESNet, self).__init__()
        self.lin_hw1 = nn.Linear(in_features=dim, out_features=dim, bias=True)
        self.lin_hw2 = nn.Linear(in_features=dim, out_features=dim, bias=True)

        self.relu = nn.LeakyReLU()

        self.dropout = torch.nn.ModuleList([nn.Dropout(r_dropout) for i in range(5)])

    def forward(self, x):

        relu_out1 = self.relu(self.lin_hw1(x))
        relu_out2 = self.lin_hw2(relu_out1)

        highway_1 = self.relu(x + relu_out2)

        return highway_1


class TopicClassifier(torch.nn.Module):

    def __init__(self, dim_in, dim_hw):
        super(TopicClassifier, self).__init__()
        self.dim_in = dim_in
        self.dim_hw = dim_hw

        self.lin_hw1 = nn.Linear(in_features=self.dim_in, out_features=self.dim_hw, bias=True)

        self.nonl1 = nn.Tanh()

        self.hw = RESNet(dim=self.dim_hw)

        self.dropout = nn.Dropout(.5)

        self.lin_softmax = nn.Linear(in_features=self.dim_hw, out_features=2, bias=True)

        self.softmax = nn.Softmax(1)

    def forward(self, feats):

        _x = self.nonl1(self.lin_hw1(feats))
        _x = self.hw(_x)
        _x = self.dropout(_x)
        _x = self.lin_softmax(_x)

        #return self.softmax(_x)
        return _x


########################################################################################################################
# TRAIN

#device_cuda = torch.device("cuda:0")
device_cpu = torch.device("cpu")

save_path = 'data/model/oai_large_dim_hw=6144_reg_v2'

model = TopicClassifier(dim_in=1536*2*2, dim_hw=1536)
model = model.to(device_cpu)

epochs = 10000
batch_size = 512
eval_every = 2000
print_every = 100

lr = .001
l2_reg = 0.01  # 0.01 good

optimizer = torch.optim.Adam([p for p in model.parameters() if p.requires_grad], lr=lr)

print('--------------', sum(p.numel() for p in model.parameters() if p.requires_grad), 'TOTAL PARAMETERS')

#loss = nn.CrossEntropyLoss(reduction='none', weight=torch.from_numpy(np.array([1., 2.5], dtype='float32')).cuda())
loss = nn.CrossEntropyLoss(reduction='mean', weight=torch.from_numpy(np.array([1., 10], dtype='float32')))

l_losses = list()

n_batches = int(ntrain / batch_size) + 1

max_eval = 0.0
kk = 0
for epoch in range(epochs):
    if epoch % 100 == 0:
        print('-------------------------------------------------- starting epoch %s' % epoch)

    for k in range(n_batches):

        if kk % eval_every == 0:

            model = model.to(device_cpu)

            model.train(False)

            x_hat = model(torch.from_numpy(valid_x))
            x_hat = x_hat.argmax(dim=1).numpy()

            acc = ((x_hat == valid_y).sum() / len(valid_y))
            TP = round(valid_y[x_hat == 1].mean(), 2)
            FN = round(1 - x_hat[valid_y == 1].mean(), 2)

            print('EVAL: ', acc, '   TP:', TP, '   FN', FN)

            if acc > max_eval:
                torch.save(model.state_dict(), save_path)
                print('---------------------- SAVING MODEL')
                max_eval = acc

            #model = model.to(device_cuda)
            model = model.to(device_cpu)

        spl = random.sample(range(ntrain), batch_size)

        batch_x = train_x[spl]
        batch_y = train_y[spl]
        batch_weights = train_weights[spl]

        #x = torch.from_numpy(batch_x).cuda()
        #y = torch.from_numpy(batch_y).cuda()

        x = torch.from_numpy(batch_x)
        y = torch.from_numpy(batch_y)

        model.train(True)
        x_hat = model(x)

        # REGULARIZATION #################################
        #l2_reg_hw = torch.tensor(0.).cuda()
        l2_reg_hw = torch.tensor(0.)
        for layer in [model.nonl1, model.lin_softmax, model.hw.lin_hw1, model.hw.lin_hw2]:  # , model.lin_hw1
        #for layer in [model.lin_softmax, model.lin_hw1]:
            for param in layer.parameters():
                l2_reg_hw += torch.norm(param)
        #######################################################

        optimizer.zero_grad()

        l = loss(x_hat, y) + l2_reg * l2_reg_hw

        #l_losses.append(l.detach().cpu().numpy())
        l_losses.append(l.detach().numpy())

        if kk % print_every == 0:
            print('loss ', sum(l_losses[-print_every:]) / min(print_every, len(l_losses)))
        kk += 1
        l.backward()

        torch.nn.utils.clip_grad_norm_(model.parameters(), 10)
        optimizer.step()

