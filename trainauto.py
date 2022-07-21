### In this file, we define the function for training the SFGAE model.

import time
import random
import numpy as np
import pandas as pd
import math
import mxnet as mx
from mxnet import ndarray as nd, gluon, autograd
from mxnet.gluon import loss as gloss
import dgl
from sklearn.model_selection import KFold
from sklearn import metrics

from utilsauto import build_graph, sample, load_data
from model import SFGAE, GraphEncoder, BilinearDecoder, BilinearDecoder_FM

#### Model training function ####
### The inputs are various hyperparameters including the embedding size, the number of layers, the dropout rate, the slope of LeaklyRelu etc.
### The outputs are various metrics including AUC, F1, Recall etc.

def Train(directory, epochs, aggregator, embedding_size, layers, dropout, slope, lr, wd, random_seed, ctx):
    dgl.load_backend('mxnet')
    random.seed(random_seed)
    np.random.seed(random_seed)
    mx.random.seed(random_seed)

    #### Build bipartite graph ####
    g, disease_ids_invmap, mirna_ids_invmap = build_graph(directory, random_seed=random_seed, ctx=ctx)
    samples = sample(directory, random_seed=random_seed)
    ID, IM = load_data(directory)


    samples_df = pd.DataFrame(samples, columns=['miRNA', 'disease', 'label'])
    sample_disease_vertices = [disease_ids_invmap[id_] for id_ in samples[:, 1]]
    sample_mirna_vertices = [mirna_ids_invmap[id_] + ID.shape[0] for id_ in samples[:, 0]]

    #### k-fold cross validation  ####
    kf = KFold(n_splits=5, shuffle=True, random_state=random_seed)
    train_index = []
    test_index = []
    
    for train_idx, test_idx in kf.split(samples[:, 2]):
        train_index.append(train_idx)
        test_index.append(test_idx)

    auc_result = []
    acc_result = []
    pre_result = []
    recall_result = []
    f1_result = []
    aupr_result = []

    fprs = []
    tprs = []

    for i in range(len(train_index)):


        samples_df['train'] = 0
        samples_df['test'] = 0

        samples_df['train'].iloc[train_index[i]] = 1
        samples_df['test'].iloc[test_index[i]] = 1

        train_tensor = nd.from_numpy(samples_df['train'].values.astype('int32')).copyto(ctx)
        test_tensor = nd.from_numpy(samples_df['test'].values.astype('int32')).copyto(ctx)

        edge_data = {'train': train_tensor,
                     'test': test_tensor}

        g.edges[sample_disease_vertices, sample_mirna_vertices].data.update(edge_data)
        g.edges[sample_mirna_vertices, sample_disease_vertices].data.update(edge_data)

        train_eid = g.filter_edges(lambda edges: edges.data['train']).astype('int64')
        g_train = g.edge_subgraph(train_eid, preserve_nodes=True)
        g_train.copy_from_parent()

        #### get the training set  ####
        rating_train = g_train.edata['rating']
        src_train, dst_train = g_train.all_edges()
        
        
        #### get the testing edge set ####
        test_eid = g.filter_edges(lambda edges: edges.data['test']).astype('int64')
        src_test, dst_test = g.find_edges(test_eid)
        rating_test = g.edges[test_eid].data['rating']
        src_train = src_train.copyto(ctx)
        src_test = src_test.copyto(ctx)
        dst_train = dst_train.copyto(ctx)
        dst_test = dst_test.copyto(ctx)

        #### SFGAE model  ####
        model = SFGAE(GraphEncoder(embedding_size=embedding_size, n_layers=layers, G=g_train, aggregator=aggregator,
                                    dropout=dropout, slope=slope, ctx=ctx),
                       BilinearDecoder(feature_size=embedding_size))

        #### model initialization  ####
        model.collect_params().initialize(init=mx.init.Xavier(rnd_type='uniform', magnitude=math.sqrt(2)), ctx=ctx)#factor_type='out', gaussian, uniform
        cross_entropy = gloss.SigmoidBinaryCrossEntropyLoss(from_sigmoid=True)
        trainer = gluon.Trainer(model.collect_params(), 'adamax', {'learning_rate': lr, 'wd': wd}) #adam,sgd,adamax,adagrad

        #### Iterative training ####

        for epoch in range(epochs):
            start = time.time()
            for _ in range(10):
                with mx.autograd.record():
                    score_train = model(g_train, src_train, dst_train)
                    loss_train = cross_entropy(score_train, rating_train).mean()
                    loss_train.backward()
                trainer.step(1)

            h_val = model.encoder(g)
            score_val = model.decoder(h_val[src_test], h_val[dst_test])
            # score_val = model.decoder_FM(h_val[src_test], h_val[dst_test])
            loss_val = cross_entropy(score_val, rating_test).mean()

            #### calculate evaluation metrics ###

            train_auc = metrics.roc_auc_score(np.squeeze(rating_train.asnumpy()), np.squeeze(score_train.asnumpy()))
            val_auc = metrics.roc_auc_score(np.squeeze(rating_test.asnumpy()), np.squeeze(score_val.asnumpy()))

            results_val = [0 if j < 0.5 else 1 for j in np.squeeze(score_val.asnumpy())]
            accuracy_val = metrics.accuracy_score(rating_test.asnumpy(), results_val)
            precision_val = metrics.precision_score(rating_test.asnumpy(), results_val)
            recall_val = metrics.recall_score(rating_test.asnumpy(), results_val)
            f1_val = metrics.f1_score(rating_test.asnumpy(), results_val)

            end = time.time()



        h_test = model.encoder(g)
        score_test = model.decoder(h_test[src_test], h_test[dst_test])

        fpr, tpr, thresholds = metrics.roc_curve(np.squeeze(rating_test.asnumpy()), np.squeeze(score_test.asnumpy()))
        test_auc = metrics.auc(fpr, tpr)

        precision1, recall1, thresholds1 = metrics.precision_recall_curve(np.squeeze(rating_test.asnumpy()), np.squeeze(score_test.asnumpy()))
        test_auPR = metrics.auc(recall1,precision1)

        results_test = [0 if j < 0.5 else 1 for j in np.squeeze(score_test.asnumpy())]
        accuracy_test = metrics.accuracy_score(rating_test.asnumpy(), results_test)
        precision_test = metrics.precision_score(rating_test.asnumpy(), results_test)
        recall_test = metrics.recall_score(rating_test.asnumpy(), results_test)
        f1_test = metrics.f1_score(rating_test.asnumpy(), results_test)

        auc_result.append(test_auc)
        acc_result.append(accuracy_test)
        pre_result.append(precision_test)
        recall_result.append(recall_test)
        f1_result.append(f1_test)
        aupr_result.append(test_auPR)

        fprs.append(fpr)
        tprs.append(tpr)



    return auc_result, acc_result, pre_result, recall_result, f1_result, fprs, tprs,aupr_result
