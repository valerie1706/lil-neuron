# use lasagne
"""
Examples that mimick the setup in http://arxiv.org/abs/1409.2329
except that we use GRU instead of a LSTM layers.
The example demonstrates:
    * How to setup GRU in Lasagne to predict a target for every position in a
      sequence.
    * How to setup Lasagne for language modelling tasks.
    * Fancy reordering of data to allow theano to process a large text
      corpus.
    * How to combine recurrent and feed-forward layers in the same Lasagne
      model.


STRATEGY:
    Each song includes a separate sub-network that's not recurrent with just metadata about the song
    so year, location, artist (embedded as a low-dimensional vector), featured artists, etc.

    Each song could addionally include the performing rapper as the start-of-verse symbol, using the same
    encoding as the artist and featured artists mentioned in the metadata

    Scrape from lyrics.wikia.com, after first scraping the artists to use from the top N rappers on
    last.fm, spotify, or echo nest, turn the artist's name into the format lyrics.wikia.com uses,
    go to the artist page there, and find the CSS class that lists all the songs (or albums) and urls
    Each song also includes an Artist: at the start of each verse if the artist is not the same as the
    artist who made the song, or who is a sub-artist (if it's a rap group)

    ALSO:
    strategy for word embeddings: since rappers already know what words mean in their usual context
    before even learning how to rap, I'll use Word2Vec to generate embeddings for the words in the corpus
    beforehand. Unknown words (mispellings or rap lingo) will be embedded by some linear combinations of
    the surrounding word vectors, maybe subtracting common words and adding uncommon words, as well as
    finding the words closest in spelling to the word in question
"""
from __future__ import print_function, division
import numpy as np
import theano
import theano.tensor as T
import os
import time
import gzip
import lasagne
import cPickle
from extract_feature import RapFeatureExtractor

np.random.seed(1234)

#  SETTINGS
folder = 'penntree'                 # subfolder with data
BATCH_SIZE = 50                     # batch size
MODEL_WORD_LEN = 50                 # how many words to unroll
                                    # (some features may have multiple
                                    #  symbols per word)
TOL = 1e-6                          # numerial stability
INI = lasagne.init.Uniform(0.1)     # initial parameter values
REC_NUM_UNITS = 400                 # number of LSTM units
embedding_size = 400                # Embedding size
dropout_frac = 0.1                  # optional recurrent dropout
lr = 2e-3                           # learning rate
decay = 2.0                         # decay factor
no_decay_epochs = 5                 # run this many epochs before first decay
max_grad_norm = 15                  # scale steps if norm is above this value
num_epochs = 1000                   # Number of epochs to run


def reorder(x_in, batch_size, model_seq_len):
    """
    Rearranges data set so batches process sequential data.
    If we have the dataset:
    x_in = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
    and the batch size is 2 and the model_seq_len is 3. Then the dataset is
    reordered such that:
                   Batch 1    Batch 2
                 ------------------------
    batch pos 1  [1, 2, 3]   [4, 5, 6]
    batch pos 2  [7, 8, 9]   [10, 11, 12]
    This ensures that we use the last hidden state of batch 1 to initialize
    batch 2.
    Also creates targets. In language modelling the target is to predict the
    next word in the sequence.
    Parameters
    ----------
    x_in : 1D numpy.array
    batch_size : int
    model_seq_len : int
        number of steps the model is unrolled
    Returns
    -------
    reordered x_in and reordered targets. Targets are shifted version of x_in.
    """
    if x_in.ndim != 1:
        raise ValueError("Data must be 1D, was", x_in.ndim)

    if x_in.shape[0] % (batch_size*model_seq_len) == 0:
        print(" x_in.shape[0] % (batch_size*model_seq_len) == 0 -> x_in is "
              "set to x_in = x_in[:-1]")
        x_in = x_in[:-1]

    x_resize =  \
        (x_in.shape[0] // (batch_size*model_seq_len))*model_seq_len*batch_size
    n_samples = x_resize // (model_seq_len)
    n_batches = n_samples // batch_size

    targets = x_in[1:x_resize+1].reshape(n_samples, model_seq_len)
    x_out = x_in[:x_resize].reshape(n_samples, model_seq_len)

    out = np.zeros(n_samples, dtype=int)

    for i in range(n_batches):
        val = range(i, n_batches*batch_size+i, n_batches)
        out[i*batch_size:(i+1)*batch_size] = val

    x_out = x_out[out]
    targets = targets[out]

    return x_out.astype('int32'), targets.astype('int32')


def traindata(model_seq_len, batch_size, vocab_map, vocab_idx):
    x = load_data(os.path.join(folder, "ptb.train.txt.gz"),
                  vocab_map, vocab_idx)
    return reorder(x, batch_size, model_seq_len)


def validdata(model_seq_len, batch_size, vocab_map, vocab_idx):
    x = load_data(os.path.join(folder, "ptb.valid.txt.gz"),
                  vocab_map, vocab_idx)
    return reorder(x, batch_size, model_seq_len)

# vocab_map and vocab_idx are updated as side effects of load_data
vocab_map = {}
vocab_idx = [0]
x_train, y_train = traindata(MODEL_SEQ_LEN, BATCH_SIZE, vocab_map, vocab_idx)
x_valid, y_valid = validdata(MODEL_SEQ_LEN, BATCH_SIZE, vocab_map, vocab_idx)
vocab_size = vocab_idx[0]


print("-" * 80)
print("Vocab size:s", vocab_size)
print("Data shapes")
print("Train data:", x_train.shape)
print("Valid data:", x_valid.shape)
print("-" * 80)

# Theano symbolic vars
sym_y = T.imatrix()




# BUILDING THE MODEL
def build_rnn(hid1_init_sym, hid2_init_sym, model_seq_len, word_vector_size):
# Model structure:
#
#    embedding --> GRU1 --> GRU2 --> output network --> predictions
    l_inp = lasagne.layers.InputLayer((BATCH_SIZE, model_seq_len))

    l_emb = lasagne.layers.EmbeddingLayer(
        l_inp,
        input_size=word_vector_size,     # word2vec embedding dimension or number of phonemes
        output_size=embedding_size,  # vector size used to represent each word internally
        W=INI)

    l_drp0 = lasagne.layers.DropoutLayer(l_emb, p=dropout_frac)


    def create_gate():
        return lasagne.layers.Gate(W_in=INI, W_hid=INI, W_cell=None)

# first GRU layer
    l_rec1 = lasagne.layers.GRULayer(
        l_drp0,
        num_units=REC_NUM_UNITS,
        resetgate=create_gate(),
        updategate=create_gate(),
        hidden_update=create_gate(),
        learn_init=False,
        hid_init=hid1_init_sym)

    l_drp1 = lasagne.layers.DropoutLayer(l_rec1, p=dropout_frac)

# Second GRU layer
    l_rec2 = lasagne.layers.GRULayer(
        l_drp1,
        num_units=REC_NUM_UNITS,
        resetgate=create_gate(),
        updategate=create_gate(),
        hidden_update=create_gate(),
        learn_init=False,
        hid_init=hid2_init_sym)

    l_drp2 = lasagne.layers.DropoutLayer(l_rec2, p=dropout_frac)
    return [l_inp1, l_emb, l_drp0, l_rec1, l_drp1, l_rec2, l_drp2]


feature_extractor = RapFeatureExtractor(data_iter)
features = feature_extractor.feature_set()
final_layers = []
rec_layers = []
input_dict = {}
inputs = []
total_model_len = 0
for f in features:
    feature_vector_size = f.vector_dim
    model_seq_len = MODEL_WORD_LEN
    total_model_len += model_seq_len

    input_X = T.imatrix()
    hid1_init_sym = T.matrix()
    hid2_init_sym = T.matrix()
    inputs.extend([input_X, hid1_init_sym, hid2_init_sym])

    [l_inp, l_emb, l_drp0, l_rec1, l_drp1, l_rec2, l_drp2] = \
        build_rnn(hid1_init_sym, hid2_init_sym,
                  model_seq_len, feature_vector_size)
    input_dict[l_inp] = input_X

    final_layers.append(l_drp2)
    rec_layers.extend(l_rec1, l_rec2)

concat_layer = ConcatLayer(final_layers, axis=1)

# by reshaping we can combine feed-forward and recurrent layers in the
# same Lasagne model.
l_shp = lasagne.layers.ReshapeLayer(concat_layer,
                                    (BATCH_SIZE*total_model_len, REC_NUM_UNITS))
l_out = lasagne.layers.DenseLayer(l_shp,
                                  num_units=vocab_size,
                                  nonlinearity=lasagne.nonlinearities.softmax)
l_out = lasagne.layers.ReshapeLayer(l_out,
                                    (BATCH_SIZE, MODEL_SEQ_LEN, vocab_size))


def calc_cross_ent(net_output, targets):
    # Helper function to calculate the cross entropy error
    preds = T.reshape(net_output, (BATCH_SIZE * MODEL_SEQ_LEN, vocab_size))
    preds += TOL  # add constant for numerical stability
    targets = T.flatten(targets)
    cost = T.nnet.categorical_crossentropy(preds, targets)
    return cost

# Note the use of deterministic keyword to disable dropout during evaluation.
y = lasagne.layers.get_output(l_out, { l_in1: x1, l_in2: x2 })
train_out_layers = lasagne.layers.get_output(
        [l_out] + rec_layers, input_dict, deterministic=False)
train_out = train_out_layers[0]


# after we have called get_ouput then the layers will have reference to
# their output values. We need to keep track of the output values for both
# training and evaluation and for each hidden layer because we want to
# initialze each batch with the last hidden values from the previous batch.
hidden_states_train = train_out_layers[1:]

eval_out, l_rec1_hid_out,  l_rec2_hid_out = lasagne.layers.get_output(
    [l_out] + rec_layers, input_dict, deterministic=True)
eval_out = eval_out_layers[0]
hidden_states_eval = eval_out_layers[1:]

cost_train = T.mean(calc_cross_ent(train_out, sym_y))
cost_eval = T.mean(calc_cross_ent(eval_out, sym_y))

# Get list of all trainable parameters in the network.
all_params = lasagne.layers.get_all_params(l_out, trainable=True)

# Calculate gradients w.r.t cost function. Note that we scale the cost with
# MODEL_SEQ_LEN. This is to be consistent with
# https://github.com/wojzaremba/lstm . The scaling is due to difference
# between torch and theano. We could have also scaled the learning rate, and
# also rescaled the norm constraint.
all_grads = T.grad(cost_train*MODEL_SEQ_LEN, all_params)

all_grads = [T.clip(g, -5, 5) for g in all_grads]

# With the gradients for each parameter we can calculate update rules for each
# parameter. Lasagne implements a number of update rules, here we'll use
# sgd and a total_norm_constraint.
all_grads, norm = lasagne.updates.total_norm_constraint(
    all_grads, max_grad_norm, return_norm=True)


# Use shared variable for learning rate. Allows us to change the learning rate
# during training.
sh_lr = theano.shared(lasagne.utils.floatX(lr))
updates = lasagne.updates.sgd(all_grads, all_params, learning_rate=sh_lr)

# Define evaluation function. This graph disables dropout.
print("compiling f_eval...")
f_eval = theano.function(inputs,
                         [cost_eval]+
                          [t[:,-1] for t in hidden_states_eval])

# define training function. This graph has dropout enabled.
# The update arg specifies that the parameters should be updated using the
# update rules.
print("compiling f_train...")
f_train = theano.function(inputs,
                          [cost_train, norm] +
                          [t[:,-1] for t in hidden_states_train],
                          updates=updates)


def calc_perplexity(x, y):
    """
    Helper function to evaluate perplexity.
    Perplexity is the inverse probability of the test set, normalized by the
    number of words.
    See: https://web.stanford.edu/class/cs124/lec/languagemodeling.pdf
    This function is largely based on the perplexity calcualtion from
    https://github.com/wojzaremba/lstm/
    """

    n_batches = x.shape[0] // BATCH_SIZE
    l_cost = []
    hid1, hid2 = [np.zeros((BATCH_SIZE, REC_NUM_UNITS),
                           dtype='float32') for _ in range(2)]

    for i in range(n_batches):
        x_batch = x[i*BATCH_SIZE:(i+1)*BATCH_SIZE]
        y_batch = y[i*BATCH_SIZE:(i+1)*BATCH_SIZE]
        cost, hid1, hid2 = f_eval(
            x_batch, y_batch, hid1, hid2)
        l_cost.append(cost)

    n_words_evaluated = (x.shape[0] - 1) / MODEL_SEQ_LEN
    perplexity = np.exp(np.sum(l_cost) / n_words_evaluated)

    return perplexity

n_batches_train = x_train.shape[0] // BATCH_SIZE
for epoch in range(num_epochs):
    l_cost, l_norm, batch_time = [], [], time.time()

    # use zero as initial state
    hidden_states = [np.zeros((BATCH_SIZE, REC_NUM_UNITS),
                           dtype='float32') for _ in range(len(features))]
    for i in range(n_batches_train):
        #x_batch = x_train[i*BATCH_SIZE:(i+1)*BATCH_SIZE]   # single batch
        #y_batch = y_train[i*BATCH_SIZE:(i+1)*BATCH_SIZE]
        x_batch, y_batch = corpus_iterator.extract_batch()
        x_valid, y_valid = corpus_iterator.extract_batch(valid=True)
        features_batch = feature_extractor.extract_batch(x_batch)
        valid_features_batch = feature_extractor.extract_batch(x_batch, valid=True)
        cost, norm, hid1, hid2 = f_train(
            features_batch, y_batch, *hidden_states)
        l_cost.append(cost)
        l_norm.append(norm)
    with open('model.pickle', 'wb') as f:
        cPickle.dump(lasagne.layers.get_all_param_values(l_out), f,
                     cPickle.HIGHEST_PROTOCOL)

    if epoch > (no_decay_epochs - 1):
        current_lr = sh_lr.get_value()
        sh_lr.set_value(lasagne.utils.floatX(current_lr / float(decay)))

    elapsed = time.time() - batch_time
    words_per_second = float(BATCH_SIZE*(MODEL_SEQ_LEN)*len(l_cost)) / elapsed
    n_words_evaluated = (x_train.shape[0] - 1) / MODEL_SEQ_LEN
    perplexity_valid = calc_perplexity(x_valid, y_valid)
    perplexity_train = np.exp(np.sum(l_cost) / n_words_evaluated)
    print("Epoch           :", epoch)
    print("Perplexity Train:", perplexity_train)
    print("Perplexity valid:", perplexity_valid)
    print("Words per second:", words_per_second)
    l_cost = []
    batch_time = 0