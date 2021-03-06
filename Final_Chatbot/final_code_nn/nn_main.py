import json
import nltk
import numpy
import torch
import torch.nn as nn
import random
import re
import os
import unicodedata
from io import open
import itertools
import torch.nn.functional as F

USE_CUDA = torch.cuda.is_available()
device = torch.device("cuda" if USE_CUDA else "cpu")

MAX_LENGTH = 10
PAD_token = 0  # Used for padding short sentences
SOS_token = 1  # Start-of-sentence tokenjw`1   \

EOS_token = 2  # End-of-sentence token


class EncoderRNN(nn.Module):
    def __init__(self, hidden_size, embedding, n_layers=1, dropout=0):
        super(EncoderRNN, self).__init__()
        self.n_layers = n_layers
        self.hidden_size = hidden_size
        self.embedding = embedding

        # Initialize GRU; the input_size and hidden_size params are both set to 'hidden_size'
        #   because our input size is a word embedding with number of features == hidden_size

        self.gru = nn.GRU(hidden_size, hidden_size, n_layers,
                          dropout=(0 if n_layers == 1 else dropout), bidirectional=True)

    def forward(self, input_seq, input_lengths, hidden=None):
        # Convert word indexes to embeddings
        embedded = self.embedding(input_seq)
        # Pack padded batch of sequences for RNN module
        packed = nn.utils.rnn.pack_padded_sequence(embedded, input_lengths)
        # Forward pass through GRU
        outputs, hidden = self.gru(packed, hidden)
        # Unpack padding
        outputs, _ = nn.utils.rnn.pad_packed_sequence(outputs)
        # Sum bidirectional GRU outputs
        outputs = outputs[:, :, :self.hidden_size] + outputs[:, :, self.hidden_size:]
        # Return output and final hidden state
        return outputs, hidden


# Luong attention layer
class Attn(nn.Module):
    def __init__(self, method, hidden_size):
        super(Attn, self).__init__()
        self.method = method
        if self.method not in ['dot', 'general', 'concat']:
            raise ValueError(self.method, "is not an appropriate attention method.")
        self.hidden_size = hidden_size
        if self.method == 'general':
            self.attn = nn.Linear(self.hidden_size, hidden_size)
        elif self.method == 'concat':
            self.attn = nn.Linear(self.hidden_size * 2, hidden_size)
            self.v = nn.Parameter(torch.FloatTensor(hidden_size))

    def dot_score(self, hidden, encoder_output):
        return torch.sum(hidden * encoder_output, dim=2)

    def general_score(self, hidden, encoder_output):
        energy = self.attn(encoder_output)
        return torch.sum(hidden * energy, dim=2)

    def concat_score(self, hidden, encoder_output):
        energy = self.attn(torch.cat((hidden.expand(encoder_output.size(0), -1, -1), encoder_output), 2)).tanh()
        return torch.sum(self.v * energy, dim=2)

    def forward(self, hidden, encoder_outputs):
        # Calculate the attention weights (energies) based on the given method
        if self.method == 'general':
            attn_energies = self.general_score(hidden, encoder_outputs)
        elif self.method == 'concat':
            attn_energies = self.concat_score(hidden, encoder_outputs)
        elif self.method == 'dot':
            attn_energies = self.dot_score(hidden, encoder_outputs)

        # Transpose max_length and batch_size dimensions
        attn_energies = attn_energies.t()

        # Return the softmax normalized probability scores (with added dimension)
        return F.softmax(attn_energies, dim=1).unsqueeze(1)

class LuongAttnDecoderRNN(nn.Module):
    def __init__(self, attn_model, embedding, hidden_size, output_size, n_layers=1, dropout=0.1):
        super(LuongAttnDecoderRNN, self).__init__()
        # ?????????self??????attn_model?????????????????????Attn???????????????
        # Keep for reference
        self.attn_model = attn_model
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.n_layers = n_layers
        self.dropout = dropout

        # Define layers
        # ??????Decoder???layers
        self.embedding = embedding
        self.embedding_dropout = nn.Dropout(dropout)
        self.gru = nn.GRU(hidden_size, hidden_size, n_layers, dropout=(0 if n_layers == 1 else dropout))
        self.concat = nn.Linear(hidden_size * 2, hidden_size)
        self.out = nn.Linear(hidden_size, output_size)

        self.attn = Attn(attn_model, hidden_size)

    def forward(self, input_step, last_hidden, encoder_outputs):
        # ?????????decoder???????????????????????????????????????????????????t??????????????????????????????t+1?????????
        # input_step???shape???(1, 64)???64???batch???1?????????????????????ID(??????????????????????????????)
        # ??????embedding?????????(1, 64, 500)???????????????dropout???shape?????????
        # Note: we run this one step (word) at a time
        # Get embedding of current input word
        embedded = self.embedding(input_step)
        embedded = self.embedding_dropout(embedded)
        # ???embedded??????GRU??????forward??????
        # ??????rnn_output???shape???(1, 64, 500)
        # hidden???(2, 64, 500)?????????????????????GRU?????????????????????2???
        # Forward through unidirectional GRU
        rnn_output, hidden = self.gru(embedded, last_hidden)
        # ???????????????????????? ????????????????????????attn_weights???shape???(64, 1, 10)
        # Calculate attention weights from the current GRU output
        attn_weights = self.attn(rnn_output, encoder_outputs)
        # encoder_outputs???(10, 64, 500)
        # encoder_outputs.transpose(0, 1)??????shape???(64, 10, 500)
        # attn_weights.bmm??????(64, 1, 500)

        # bmm???????????????????????????????????????batch??????????????????attn_weights??????64???(1,10)?????????
        # ???encoder_outputs.transpose(0, 1)??????64???(10, 500)?????????
        # ??????bmm??????64???(1, 10)?????? x (10, 500)?????????????????????(64, 1, 500)
        # Multiply attention weights to encoder outputs to get new "weighted sum" context vector
        context = attn_weights.bmm(encoder_outputs.transpose(0, 1))
        # ???context?????????GRU?????????????????????
        # rnn_output???(1, 64, 500)??????(64, 500)
        # Concatenate weighted context vector and GRU output using Luong eq. 5
        rnn_output = rnn_output.squeeze(0)
        # context???(64, 1, 500)??????(64, 500)
        context = context.squeeze(1)
        # ????????????(64, 1000)
        concat_input = torch.cat((rnn_output, context), 1)
        # self.concat???????????????(1000, 500)???
        # self.concat(concat_input)????????????(64, 500)
        # ?????????tanh?????????????????????(-1,1)???concat_output???shape???(64, 500)
        concat_output = torch.tanh(self.concat(concat_input))
        # out???(500, ????????????=7826)
        # Predict next word using Luong eq. 6
        output = self.out(concat_output)
        # ???softmax????????????????????????????????????????????????????????????
        output = F.softmax(output, dim=1)
        # ?????? output??????????????????
        # Return output and final hidden state
        return output, hidden

def maskNLLLoss(inp, target, mask):
    # ????????????????????????????????????padding???0??????padding???1?????????sum???????????????????????????
    nTotal = mask.sum()

    crossEntropy = -torch.log(torch.gather(inp, 1, target.view(-1, 1)).squeeze(1))
    loss = crossEntropy.masked_select(mask).mean()
    loss = loss.to(device)
    return loss, nTotal.item()


class Voc:
    def __init__(self, name):
        self.name = name
        self.trimmed = False
        self.word2index = {}
        self.word2count = {}
        self.index2word = {PAD_token: "PAD", SOS_token: "SOS", EOS_token: "EOS"}
        self.num_words = 3  # Count SOS, EOS, PAD

    def addSentence(self, sentence):
        for word in sentence.split(' '):
            self.addWord(word)

    def addWord(self, word):
        if word not in self.word2index:
            self.word2index[word] = self.num_words
            self.word2count[word] = 1
            self.index2word[self.num_words] = word
            self.num_words += 1
        else:
            self.word2count[word] += 1

    # Remove words below a certain count threshold
    def trim(self, min_count):
        if self.trimmed:
            return
        self.trimmed = True

        keep_words = []

        for k, v in self.word2count.items():
            if v >= min_count:
                keep_words.append(k)

        print('keep_words {} / {} = {:.4f}'.format(
            len(keep_words), len(self.word2index), len(keep_words) / len(self.word2index)
        ))

        # Reinitialize dictionaries
        self.word2index = {}
        self.word2count = {}
        self.index2word = {PAD_token: "PAD", SOS_token: "SOS", EOS_token: "EOS"}
        self.num_words = 3 # Count default tokens

        for word in keep_words:
            self.addWord(word)


def indexesFromSentence(voc, sentence):
    lis = []
    flag = False
    for word in sentence.split(' '):
        if word in voc.word2index:
            lis.append(voc.word2index[word])
        else:
            flag = True
    if not flag:
        # return [voc.word2index[word] for word in sentence.split(' ')] + [EOS_token]
        return lis + [EOS_token]
    else:
        return []


def zeroPadding(l, fillvalue=PAD_token):
    return list(itertools.zip_longest(*l, fillvalue=fillvalue))

def binaryMatrix(l, value=PAD_token):
    m = []
    for i, seq in enumerate(l):
        m.append([])
        for token in seq:
            if token == PAD_token:
                m[i].append(0)
            else:
                m[i].append(1)
    return m


def inputVar(l, voc):
    indexes_batch = [indexesFromSentence(voc, sentence) for sentence in l]
    lengths = torch.tensor([len(indexes) for indexes in indexes_batch])
    padList = zeroPadding(indexes_batch)
    padVar = torch.LongTensor(padList)
    return padVar, lengths

# Returns padded target sequence tensor, padding mask, and max target length
def outputVar(l, voc):
    indexes_batch = [indexesFromSentence(voc, sentence) for sentence in l]
    max_target_len = max([len(indexes) for indexes in indexes_batch])
    padList = zeroPadding(indexes_batch)
    mask = binaryMatrix(padList)
    mask = torch.BoolTensor(mask)
    padVar = torch.LongTensor(padList)
    return padVar, mask, max_target_len

def batch2TrainData(voc, pair_batch):
    pair_batch.sort(key=lambda x: len(x[0].split(" ")), reverse=True)
    input_batch, output_batch = [], []
    for pair in pair_batch:
        input_batch.append(pair[0])
        output_batch.append(pair[1])
    inp, lengths = inputVar(input_batch, voc)
    output, mask, max_target_len = outputVar(output_batch, voc)
    return inp, lengths, output, mask, max_target_len




def readVocs(datafile, corpus_name):
    # Read the file and split into lines
    lines = open(datafile, encoding='utf-8').\
        read().strip().split('\n')
    # Split every line into pairs and normalize
    pairs = [[normalizeString(s) for s in l.split('\t')] for l in lines]
    voc = Voc(corpus_name)
    return voc, pairs

# Returns True iff both sentences in a pair 'p' are under the MAX_LENGTH threshold
def filterPair(p):
    # Input sequences need to preserve the last word for EOS token
    return len(p[0].split(' ')) < MAX_LENGTH and len(p[1].split(' ')) < MAX_LENGTH

# Filter pairs using filterPair condition
def filterPairs(pairs):
    return [pair for pair in pairs if filterPair(pair)]
def unicodeToAscii(s):
    return ''.join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )

# Lowercase, trim, and remove non-letter characters
def normalizeString(s):
    s = unicodeToAscii(s.lower().strip())
    s = re.sub(r"([.!?])", r" \1", s)
    s = re.sub(r"[^a-zA-Z.!?]+", r" ", s)
    s = re.sub(r"\s+", r" ", s).strip()
    return s


def loadPrepareData(corpus, corpus_name, datafile, save_dir):
    voc, pairs = readVocs(datafile, corpus_name)
    pairs = filterPairs(pairs)
    for pair in pairs:
        voc.addSentence(pair[0])
        voc.addSentence(pair[1])
    return voc, pairs


corpus_name = "cornell movie-dialogs corpus" #??????????????????
corpus = os.path.join("data", corpus_name)
# Load/Assemble voc and pairs
datafile = os.path.join(corpus, "formatted_movie_lines.txt")
datafile = 'C:\\Users\\86138\\Desktop\\Chatbot\\final_code_nn\\' + datafile
save_dir = os.path.join("data", "save")
save_dir = 'C:\\Users\\86138\\Desktop\\Chatbot\\final_code_nn\\' + save_dir
voc, pairs = loadPrepareData(corpus, corpus_name, datafile, save_dir)


model_name = 'cb_model'
attn_model = 'dot'
#attn_model = 'general'
#attn_model = 'concat'
hidden_size = 500
encoder_n_layers = 2
decoder_n_layers = 2
dropout = 0.1
batch_size = 64

# Set checkpoint to load from; set to None if starting from scratch
#loadFilename = None
checkpoint_iter = 4000
loadFilename = os.path.join(save_dir, '{}_checkpoint.tar'.format(checkpoint_iter))


# Load model if a loadFilename is provided
if loadFilename:
    # If loading on same machine the model was trained on
    #checkpoint = torch.load(loadFilename)
    # If loading a model trained on GPU to CPU
    checkpoint = torch.load(loadFilename, map_location=torch.device('cpu'))
    encoder_sd = checkpoint['en']
    decoder_sd = checkpoint['de']
    encoder_optimizer_sd = checkpoint['en_opt']
    decoder_optimizer_sd = checkpoint['de_opt']
    embedding_sd = checkpoint['embedding']
    voc.__dict__ = checkpoint['voc_dict']


class GreedySearchDecoder(nn.Module):
    def __init__(self, encoder, decoder):
        super(GreedySearchDecoder, self).__init__()
        self.encoder = encoder
        self.decoder = decoder

    def forward(self, input_seq, input_length, max_length):
        # Encoder???Forward??????
        encoder_outputs, encoder_hidden = self.encoder(input_seq, input_length)
        # ???Encoder??????????????????????????????Decoder????????????
        decoder_hidden = encoder_hidden[:decoder.n_layers]
        # ?????????????????????????????????(time,batch)????????????????????????????????????????????????????????????
        # Decoder??????????????????SOS
        decoder_input = torch.ones(1, 1, device=device, dtype=torch.long) * SOS_token
        # ???????????????????????????tensor
        all_tokens = torch.zeros([0], device=device, dtype=torch.long)
        all_scores = torch.zeros([0], device=device)
        # ???????????????????????????????????????????????????????????????EOS????????????
        for _ in range(max_length):
            # Decoder forward??????
            decoder_output, decoder_hidden = self.decoder(decoder_input, decoder_hidden,
								encoder_outputs)
            # decoder_outputs???(batch=1, vob_size)
            # ??????max?????????????????????????????????
            decoder_scores, decoder_input = torch.max(decoder_output, dim=1)
            # ????????????????????????all_tokens???all_scores???
            all_tokens = torch.cat((all_tokens, decoder_input), dim=0)
            all_scores = torch.cat((all_scores, decoder_scores), dim=0)
            # decoder_input??????????????????????????????ID????????????????????????????????????max??????????????????
            # ??????decoder???????????????batch??????????????????unsqueeze??????batch?????????
            decoder_input = torch.unsqueeze(decoder_input, 0)
        # ??????????????????????????????
        return all_tokens, all_scores

def evaluate(encoder, decoder, searcher, voc, sentence, max_length=MAX_LENGTH):
    ### ??????????????????batch????????????id
    lis = indexesFromSentence(voc, sentence)
    # indexes_batch = [indexesFromSentence(voc, sentence)]
    if lis == []:
        return -1
    else:
       indexes_batch = [lis]
    print(3333)
    # ??????lengths tensor
    lengths = torch.tensor([len(indexes) for indexes in indexes_batch])
    print(2)
    # ??????
    input_batch = torch.LongTensor(indexes_batch).transpose(0, 1)
    print(3)
    # ????????????????????????(??????GPU)
    input_batch = input_batch.to(device)
    lengths = lengths.to(device)
    # ???searcher??????
    tokens, scores = searcher(input_batch, lengths, max_length)
    print(4)
    # ID????????????
    decoded_words = [voc.index2word[token.item()] for token in tokens]
    return decoded_words


def evaluateInput(encoder, decoder, searcher, voc, input_sen):
    input_sentence = ''
    try:
        # ???????????????????????????
        input_sentence = input_sen
        #print(input_sentence)
        # ???????????????
        input_sentence = normalizeString(input_sentence)
        # ????????????Evaluate sentence
        output_words = evaluate(encoder, decoder, searcher, voc, input_sentence)
        #print(output_words)
        # ??????EOS???????????????
        if output_words != -1:
            words = []
            for word in output_words:
                if word == 'EOS':
                    break
                elif word != 'PAD':
                    words.append(word)
            # print('Alice:', ' '.join(words))
            return ' '.join(words)
        else:
            return 'At your service.'

    except KeyError:
        print("Error: Encountered unknown word.")

embedding = nn.Embedding(voc.num_words, hidden_size)
if loadFilename:
    embedding.load_state_dict(embedding_sd)
# Initialize encoder & decoder models
encoder = EncoderRNN(hidden_size, embedding, encoder_n_layers, dropout)
decoder = LuongAttnDecoderRNN(attn_model, embedding, hidden_size, voc.num_words, decoder_n_layers, dropout)
if loadFilename:
    encoder.load_state_dict(encoder_sd)
    decoder.load_state_dict(decoder_sd)
# Use appropriate device
encoder = encoder.to(device)
decoder = decoder.to(device)

# ??????eval?????????????????????dropout???
encoder.eval()
decoder.eval()

# ??????searcher??????
searcher = GreedySearchDecoder(encoder, decoder)

with open('C:\\Users\\86138\\Desktop\\Chatbot\\final_code_nn\\'+'traindata.json', 'r') as f:
    TrainArray = json.load(f)

FILE = 'C:\\Users\\86138\\Desktop\\Chatbot\\final_code_nn\\' + "data.pth"
data = torch.load(FILE)

input_size = data["input_size"]
hidden_size = data["hidden_size"]
output_size = data["output_size"]
words = data['words']
words_weight = data['words_weight']
tags = data['tags']
model_state = data["model_state"]


class ChatNN(nn.Module):
    # ???????????????????????????forward??????
    def __init__(self, input_size, hidden_size, output_size):
        super(ChatNN, self).__init__()   # ??????????????????????????????????????????????????????
        self.linear0 = nn.Linear(input_size, hidden_size)
        self.linear1 = nn.Linear(hidden_size, hidden_size)
        self.linear2 = nn.Linear(hidden_size, output_size)
        self.relu = nn.ReLU()

    def forward(self, input_x):
        # ?????????????????????
        output_mid = self.linear0(input_x)
        output_mid = self.relu(output_mid)
        output_mid = self.linear1(output_mid)
        output_mid = self.relu(output_mid)
        output_mid = self.linear1(output_mid)
        output_mid = self.relu(output_mid)
        output_mid = self.linear1(output_mid)
        output_mid = self.relu(output_mid)
        output_final = self.linear2(output_mid)
        return output_final


chatnn = ChatNN(input_size, hidden_size, output_size).to(device)
chatnn.load_state_dict(model_state)
chatnn.eval()


def trans_to_num(sen):
    # ?????????sentence?????????????????????????????????
    num_tmp = numpy.zeros(shape=len(words), dtype=numpy.float32)
    # numpy???????????????????????????????????????????????????
    for index in range(len(words)):
        if words[index] in sen:
            num_tmp[index] = words_weight[index]
    return num_tmp


print("Alice: Hello!")


# while True:
def getreply(your_sentence):
    # print('User: ' + your_sentence)
    # print(1)
    # your_sentence = input()
    # print(your_sentence)
    your_sentence = your_sentence.lower()
    sentence = nltk.word_tokenize(your_sentence)
    # print(2)
    X = trans_to_num(sentence)
    X = X.reshape(1, X.shape[0])
    X = torch.from_numpy(X).to(device)
    # print(3)
    output = chatnn(X)
    tmp, index = torch.max(output, dim=1)
    index = index.item()

    tag = tags[index]
    # print(4)
    ProbsVector = torch.softmax(output, dim=1)
    # print(5)
    # print(output)
    # print(probs)
    # print(probs[0])
    prob = ProbsVector[0][index].item() # ??????[0]
    print(prob)
    # return  evaluateInput(encoder, decoder, searcher, voc, your_sentence)
    if prob > 0.80:
        print(222)
        for i in TrainArray['traindata']:
            if tag == i["tag"]:
                # print("Alice: ", random.choice(i['responses']))
                return random.choice(i['responses'])
    else:
        #print(your_sentence)
        return evaluateInput(encoder, decoder, searcher, voc, your_sentence)

if __name__ == '__main__':
    message = 'Hello'
    print(getreply(message))
