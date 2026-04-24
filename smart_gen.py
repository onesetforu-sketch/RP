import numpy as np
import os
import logging
import random
import json
import threading

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_CACHE = os.path.join(BASE_DIR, "lstm_model_cache.json")

_tf_loaded = False
_tf = None
_model = None
_model_lock = threading.Lock()
_digit_freqs = None
_transition_matrix = None
_trained = False
_training_cards = []


def _load_tf():
    global _tf_loaded, _tf
    if _tf_loaded:
        return _tf
    try:
        os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
        import tensorflow as tf
        tf.get_logger().setLevel('ERROR')
        _tf = tf
        _tf_loaded = True
        logger.info("TensorFlow loaded successfully")
        return tf
    except Exception as e:
        logger.warning(f"TensorFlow not available: {e}")
        _tf_loaded = True
        return None


def is_luhn_valid(card_no):
    digits = [int(d) for d in str(card_no)]
    checksum = digits[-1]
    payload = digits[:-1]
    total = 0
    for i, digit in enumerate(reversed(payload)):
        if i % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return (total + checksum) % 10 == 0


def _compute_luhn_check(card_15):
    digits = [int(d) for d in str(card_15)]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - (total % 10)) % 10


def _load_cards_from_files():
    cards = []
    for fname in ["approved.txt", "livescc.txt"]:
        fpath = os.path.join(BASE_DIR, fname)
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split('|')
                    if len(parts) >= 4:
                        cc = parts[0].strip()
                        if len(cc) >= 15 and cc.isdigit():
                            cards.append(cc)
        except Exception as e:
            logger.warning(f"Error loading {fname}: {e}")
    unique = list(set(cards))
    logger.info(f"Smart gen loaded {len(unique)} unique cards for training")
    return unique


def _build_statistical_model(cards):
    global _digit_freqs, _transition_matrix
    if not cards:
        return

    max_len = max(len(c) for c in cards)
    _digit_freqs = np.zeros((max_len, 10), dtype=np.float32)
    _transition_matrix = np.zeros((max_len, 10, 10), dtype=np.float32)

    for card in cards:
        digits = [int(d) for d in card]
        for pos, d in enumerate(digits):
            _digit_freqs[pos][d] += 1
            if pos > 0:
                prev = digits[pos - 1]
                _transition_matrix[pos][prev][d] += 1

    for pos in range(_digit_freqs.shape[0]):
        row_sum = _digit_freqs[pos].sum()
        if row_sum > 0:
            _digit_freqs[pos] /= row_sum

    for pos in range(_transition_matrix.shape[0]):
        for prev in range(10):
            row_sum = _transition_matrix[pos][prev].sum()
            if row_sum > 0:
                _transition_matrix[pos][prev] /= row_sum


def _build_lstm_model(tf):
    model = tf.keras.Sequential([
        tf.keras.layers.Embedding(input_dim=10, output_dim=8),
        tf.keras.layers.LSTM(64, return_sequences=True),
        tf.keras.layers.LSTM(32),
        tf.keras.layers.Dense(10, activation='softmax')
    ])
    model.compile(loss='sparse_categorical_crossentropy', optimizer='adam')
    return model


def _prepare_training_data(cards):
    X_list = []
    y_list = []
    for card in cards:
        digits = [int(d) for d in card]
        for i in range(6, len(digits) - 1):
            seq = digits[:i]
            while len(seq) < 15:
                seq = seq + [0]
            seq = seq[:15]
            X_list.append(seq)
            y_list.append(digits[i])
    if not X_list:
        return None, None
    return np.array(X_list, dtype=np.int32), np.array(y_list, dtype=np.int32)


def _train_model(cards):
    global _model, _trained, _training_cards
    tf = _load_tf()
    _training_cards = cards

    _build_statistical_model(cards)

    if tf is None:
        logger.info("Using statistical model only (TF unavailable)")
        _trained = True
        return True

    if len(cards) < 10:
        logger.info(f"Only {len(cards)} cards, using statistical model only")
        _trained = True
        return True

    try:
        X, y = _prepare_training_data(cards)
        if X is None or len(X) < 5:
            logger.info("Not enough training sequences, using statistical model")
            _trained = True
            return True

        old_model = _model
        if old_model is not None:
            try:
                tf.keras.backend.clear_session()
                del old_model
            except Exception:
                pass

        logger.info(f"Training LSTM on {len(X)} sequences from {len(cards)} cards...")
        _model = _build_lstm_model(tf)
        _model.fit(X, y, epochs=15, batch_size=32, verbose=0)
        logger.info("LSTM training complete")
        _trained = True
        return True
    except Exception as e:
        logger.warning(f"LSTM training failed: {e}, using statistical fallback")
        _trained = True
        return True


def init_smart_gen():
    global _trained
    with _model_lock:
        if _trained:
            return
        cards = _load_cards_from_files()
        _train_model(cards)


def retrain(new_cards=None):
    global _trained
    with _model_lock:
        cards = _load_cards_from_files()
        if new_cards:
            cards.extend(new_cards)
        cards = list(set(cards))
        _trained = False
        _train_model(cards)


def generate_card_lstm(bin_prefix):
    global _model, _digit_freqs, _transition_matrix

    if not _trained:
        init_smart_gen()

    is_amex = bin_prefix.replace('x', '').startswith(('34', '37'))
    target_len = 15 if is_amex else 16

    current_seq = []
    for c in bin_prefix:
        if c == 'x':
            current_seq.append(random.randint(0, 9))
        else:
            current_seq.append(int(c))

    needed = target_len - 1 - len(current_seq)
    if needed <= 0:
        current_seq = current_seq[:target_len - 1]
    else:
        for pos_idx in range(len(current_seq), target_len - 1):
            next_digit = _predict_next_digit(current_seq, pos_idx)
            current_seq.append(next_digit)

    check = _compute_luhn_check("".join(map(str, current_seq)))
    current_seq.append(check)

    card = "".join(map(str, current_seq))
    if not is_luhn_valid(card):
        for last in range(10):
            test = "".join(map(str, current_seq[:-1])) + str(last)
            if is_luhn_valid(test):
                card = test
                break

    return card


def _predict_next_digit(current_seq, position):
    lstm_pred = None
    stat_pred = None

    if _model is not None:
        try:
            input_seq = list(current_seq)
            while len(input_seq) < 15:
                input_seq.append(0)
            input_seq = input_seq[:15]
            input_data = np.array([input_seq], dtype=np.int32)
            prediction = _model.predict(input_data, verbose=0)
            lstm_pred = prediction[0]
        except Exception:
            pass

    if _digit_freqs is not None and position < _digit_freqs.shape[0]:
        stat_pred = np.zeros(10, dtype=np.float32)

        freq_weight = _digit_freqs[position].copy()

        if _transition_matrix is not None and len(current_seq) > 0:
            prev_digit = current_seq[-1]
            trans_weight = _transition_matrix[position][prev_digit].copy()
            if trans_weight.sum() > 0:
                stat_pred = 0.4 * freq_weight + 0.6 * trans_weight
            else:
                stat_pred = freq_weight
        else:
            stat_pred = freq_weight

    if lstm_pred is not None and stat_pred is not None:
        combined = 0.6 * lstm_pred + 0.4 * stat_pred
    elif lstm_pred is not None:
        combined = lstm_pred
    elif stat_pred is not None:
        combined = stat_pred
    else:
        return random.randint(0, 9)

    total = combined.sum()
    if total <= 0:
        return random.randint(0, 9)
    combined /= total

    temperature = 0.7
    combined = np.power(combined, 1.0 / temperature)
    combined /= combined.sum()

    try:
        digit = np.random.choice(10, p=combined)
    except Exception:
        digit = random.randint(0, 9)

    return int(digit)


def generate_smart_batch(bins, count=40):
    if not _trained:
        init_smart_gen()

    results = []
    attempts = 0
    max_attempts = count * 3

    while len(results) < count and attempts < max_attempts:
        attempts += 1
        bin_choice = random.choice(bins)
        card = generate_card_lstm(bin_choice)
        if card and is_luhn_valid(card):
            results.append(card)

    return results
