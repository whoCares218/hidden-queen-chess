from flask import Flask, render_template_string, request, jsonify, session
from flask_socketio import SocketIO, join_room, emit
import uuid, json, copy, random, string, threading, time as _time, os

app = Flask(__name__)
app.secret_key = 'hidden-queen-secret-2024'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ── Game state ────────────────────────────────────────────────────────────────
rooms = {}   # room_id -> GameState dict

def new_board():
    """Standard 8×8 board. Pieces: R N B Q K B N R / p p p p p p p p"""
    b = [[None]*8 for _ in range(8)]
    back = ['R','N','B','Q','K','B','N','R']
    for c in range(8):
        b[0][c] = {'color':'black', 'type': back[c], 'id': f'b{c}'}
        b[1][c] = {'color':'black', 'type':'P', 'id': f'bp{c}', 'hidden_queen': False}
        b[6][c] = {'color':'white', 'type':'P', 'id': f'wp{c}', 'hidden_queen': False}
        b[7][c] = {'color':'white', 'type': back[c], 'id': f'w{c}'}
    return b

def init_room(room_id, time_control_secs=None):
    rooms[room_id] = {
        'board': new_board(),
        'players': {},        # sid -> 'white'|'black'
        'sids': {},           # 'white'|'black' -> sid
        'turn': 'white',
        'phase': 'waiting',   # waiting | selecting | playing | gameover
        'selected': {},       # color -> {row,col} of hidden queen pawn
        'revealed': {'white': False, 'black': False},
        'winner': None,
        'loser': None,
        'check': None,
        'last_move': None,
        'en_passant': None,
        'castling': {
            'white': {'kingside': True, 'queenside': True},
            'black': {'kingside': True, 'queenside': True}
        },
        'time_control': time_control_secs,   # None = unlimited
        'clocks': {'white': time_control_secs, 'black': time_control_secs},
        'clock_turn_start': None,            # epoch float, set when game starts
        'ai_thinking': False,                # FIX: guard against multiple AI move tasks
    }

SELECTION_TIMEOUT = 15  # seconds

def assign_random_queen(state, color):
    """Pick a random unassigned pawn and mark it as the hidden queen."""
    board = state['board']
    pawn_row = 6 if color == 'white' else 1
    cols = list(range(8))
    random.shuffle(cols)
    for c in cols:
        p = board[pawn_row][c]
        if p and p['type'] == 'P' and p['color'] == color and not p.get('hidden_queen'):
            p['hidden_queen'] = True
            state['selected'][color] = {'row': pawn_row, 'col': c}
            return

def broadcast_game_start(room_id):
    """Transition room to playing phase and push state to both players."""
    if room_id not in rooms:
        return
    state = rooms[room_id]
    if state['phase'] != 'selecting':
        return
    state['phase'] = 'playing'
    state['clock_turn_start'] = _time.time()  # white moves first
    _push_state(room_id)

def _push_state(room_id):
    """Send current game state to all human players in the room."""
    if room_id not in rooms:
        return
    state = rooms[room_id]
    for sid, c in state['players'].items():
        # FIX: removed unused 'selected' argument from board_view
        bv = board_view(state['board'], c, state['revealed'])
        socketio.emit('game_state', {
            'board': bv,
            'turn': state['turn'],
            'phase': state['phase'],
            'revealed': state['revealed'],
            'check': state['check'],
            'last_move': state['last_move'],
            'winner': state['winner'],
            'loser': state.get('loser'),
            'clocks': state['clocks'],
            'clock_turn_start': state['clock_turn_start'],
            'is_ai_game': bool(state.get('ai_color')),
        }, to=sid)
    # FIX: guard against multiple concurrent AI move tasks with ai_thinking flag
    if (state.get('ai_color') and
            state['phase'] == 'playing' and
            state['turn'] == state['ai_color'] and
            not state.get('ai_thinking')):
        state['ai_thinking'] = True
        schedule_ai_move(room_id)

def selection_timer_fired(room_id):
    """Called after 15 s: auto-assign any missing hidden queens, then start."""
    # FIX: removed unnecessary socketio.start_background_task wrapper;
    # this already runs in a threading.Timer thread.
    if room_id not in rooms:
        return
    state = rooms[room_id]
    if state['phase'] != 'selecting':
        return
    for color in ('white', 'black'):
        if color not in state['selected']:
            assign_random_queen(state, color)
            sid = state['sids'].get(color)
            if sid:
                socketio.emit('auto_selected', {}, to=sid)
    broadcast_game_start(room_id)

# FIX: removed unused 'selected' parameter — it was never referenced inside the function
def board_view(board, viewer_color, revealed):
    """Return board as seen by viewer: hide opponent's hidden queen."""
    view = []
    for r in range(8):
        row = []
        for c in range(8):
            p = board[r][c]
            if p is None:
                row.append(None)
            else:
                piece = dict(p)
                opp = 'black' if viewer_color == 'white' else 'white'
                if (piece['color'] == opp and
                        piece.get('hidden_queen') and
                        not revealed.get(opp)):
                    piece = {k: v for k, v in piece.items() if k != 'hidden_queen'}
                    piece['type'] = 'P'
                row.append(piece)
        view.append(row)
    return view

# ── Move validation ───────────────────────────────────────────────────────────

def find_king(board, color):
    for r in range(8):
        for c in range(8):
            p = board[r][c]
            if p and p['color'] == color and p['type'] == 'K':
                return (r, c)
    return None

def is_in_check(board, color):
    king = find_king(board, color)
    if not king:
        return False
    opp = 'black' if color == 'white' else 'white'
    for r in range(8):
        for c in range(8):
            p = board[r][c]
            if p and p['color'] == opp:
                if king in raw_moves(board, r, c, None):
                    return True
    return False

def raw_moves(board, fr, fc, en_passant):
    p = board[fr][fc]
    if not p:
        return []
    t, color = p['type'], p['color']
    moves = []
    is_hidden_q = p.get('hidden_queen', False)

    # FIX: simplified redundant condition `if t == 'P' or (t == 'P' and is_hidden_q)`
    if t == 'P':
        d = -1 if color == 'white' else 1
        start_row = 6 if color == 'white' else 1
        if 0 <= fr+d < 8 and board[fr+d][fc] is None:
            moves.append((fr+d, fc))
            if fr == start_row and board[fr+2*d][fc] is None:
                moves.append((fr+2*d, fc))
        for dc in [-1, 1]:
            nr, nc = fr+d, fc+dc
            if 0 <= nr < 8 and 0 <= nc < 8:
                target = board[nr][nc]
                if target and target['color'] != color:
                    moves.append((nr, nc))
                if en_passant and (nr, nc) == en_passant:
                    moves.append((nr, nc))

    if t in ('R', 'Q') or (is_hidden_q and t == 'P'):
        for dr, dc in [(1,0),(-1,0),(0,1),(0,-1)]:
            nr, nc = fr+dr, fc+dc
            while 0 <= nr < 8 and 0 <= nc < 8:
                target = board[nr][nc]
                if target:
                    if target['color'] != color:
                        moves.append((nr, nc))
                    break
                moves.append((nr, nc))
                nr += dr; nc += dc

    if t in ('B', 'Q') or (is_hidden_q and t == 'P'):
        for dr, dc in [(1,1),(1,-1),(-1,1),(-1,-1)]:
            nr, nc = fr+dr, fc+dc
            while 0 <= nr < 8 and 0 <= nc < 8:
                target = board[nr][nc]
                if target:
                    if target['color'] != color:
                        moves.append((nr, nc))
                    break
                moves.append((nr, nc))
                nr += dr; nc += dc

    if t == 'N':
        for dr, dc in [(-2,-1),(-2,1),(-1,-2),(-1,2),(1,-2),(1,2),(2,-1),(2,1)]:
            nr, nc = fr+dr, fc+dc
            if 0 <= nr < 8 and 0 <= nc < 8:
                target = board[nr][nc]
                if not target or target['color'] != color:
                    moves.append((nr, nc))

    if t == 'K':
        for dr in [-1,0,1]:
            for dc in [-1,0,1]:
                if dr == 0 and dc == 0: continue
                nr, nc = fr+dr, fc+dc
                if 0 <= nr < 8 and 0 <= nc < 8:
                    target = board[nr][nc]
                    if not target or target['color'] != color:
                        moves.append((nr, nc))

    return moves

# FIX: added en_passant parameter so en passant moves are correctly identified
# as normal pawn moves and do NOT trigger hidden queen revelation.
def is_queen_like_move(board, fr, fc, tr, tc, en_passant=None):
    dr = tr - fr
    dc = tc - fc
    color = board[fr][fc]['color']
    d = -1 if color == 'white' else 1
    start_row = 6 if color == 'white' else 1
    # Normal one-step forward pawn move
    if dc == 0 and dr == d and board[tr][tc] is None:
        return False
    # Normal two-step pawn advance from starting row
    if dc == 0 and dr == 2*d and fr == start_row and board[tr][tc] is None:
        return False
    # Normal diagonal pawn capture (target occupied)
    if abs(dc) == 1 and dr == d and board[tr][tc] is not None:
        return False
    # FIX: en passant is a normal pawn diagonal capture — NOT a queen-like move.
    # Previously this was missing, causing the hidden queen to be revealed on en passant.
    if abs(dc) == 1 and dr == d and en_passant and (tr, tc) == en_passant:
        return False
    return True

def legal_moves(state, fr, fc):
    board = state['board']
    p = board[fr][fc]
    if not p:
        return []
    color = p['color']
    moves = raw_moves(board, fr, fc, state.get('en_passant'))
    legal = []
    castling = state.get('castling', {})

    if p['type'] == 'K':
        row = 7 if color == 'white' else 0
        if fr == row and fc == 4:
            if castling.get(color, {}).get('kingside'):
                if (board[row][5] is None and board[row][6] is None and
                        not is_in_check(board, color)):
                    b2 = copy.deepcopy(board)
                    b2[row][5] = b2[row][4]; b2[row][4] = None
                    if not is_in_check(b2, color):
                        b2[row][6] = b2[row][5]; b2[row][5] = None
                        if not is_in_check(b2, color):
                            moves.append((row, 6))
            if castling.get(color, {}).get('queenside'):
                if (board[row][3] is None and board[row][2] is None and
                        board[row][1] is None and not is_in_check(board, color)):
                    b2 = copy.deepcopy(board)
                    b2[row][3] = b2[row][4]; b2[row][4] = None
                    if not is_in_check(b2, color):
                        b2[row][2] = b2[row][3]; b2[row][3] = None
                        if not is_in_check(b2, color):
                            moves.append((row, 2))

    for tr, tc in moves:
        b2 = copy.deepcopy(board)
        apply_move_to_board(b2, fr, fc, tr, tc, state.get('en_passant'), promote_to='Q')
        if not is_in_check(b2, color):
            legal.append((tr, tc))
    return legal

def apply_move_to_board(board, fr, fc, tr, tc, en_passant, promote_to='Q'):
    p = board[fr][fc]
    color = p['color']
    d = -1 if color == 'white' else 1

    if p['type'] in ('P',) and abs(tc - fc) == 1 and board[tr][tc] is None:
        if en_passant and (tr, tc) == en_passant:
            board[tr - d][tc] = None

    board[tr][tc] = p
    board[fr][fc] = None

    if p['type'] == 'K':
        row = 7 if color == 'white' else 0
        if fr == row and fc == 4:
            if tc == 6:
                board[row][5] = board[row][7]
                board[row][7] = None
            elif tc == 2:
                board[row][3] = board[row][0]
                board[row][0] = None

    # FIX: merged dead elif into the single if-block.
    # Previously the `elif p.get('hidden_queen') and p['type'] == 'P'` branch
    # could NEVER execute because `p['type'] == 'P'` is already required by the
    # `if` above, meaning the elif was unreachable dead code.
    if p['type'] == 'P' and (tr == 0 or tr == 7):
        board[tr][tc] = {'color': color, 'type': promote_to,
                         'id': p['id'], 'hidden_queen': False}

def do_move(state, fr, fc, tr, tc):
    board = state['board']
    p = board[fr][fc]
    color = p['color']
    en_passant = state.get('en_passant')
    new_ep = None

    revealed = False
    if p.get('hidden_queen') and p['type'] == 'P':
        # FIX: pass en_passant so en passant moves are not mistaken for queen moves
        if is_queen_like_move(board, fr, fc, tr, tc, en_passant):
            p['type'] = 'Q'
            p['hidden_queen'] = False
            state['revealed'][color] = True
            revealed = True

    d = -1 if color == 'white' else 1
    if p['type'] == 'P' and abs(tr - fr) == 2:
        new_ep = (fr + d, fc)

    castling = state.setdefault('castling', {
        'white': {'kingside': True, 'queenside': True},
        'black': {'kingside': True, 'queenside': True}
    })
    if p['type'] == 'K':
        castling[color]['kingside'] = False
        castling[color]['queenside'] = False
    if p['type'] == 'R':
        row = 7 if color == 'white' else 0
        if fr == row and fc == 7: castling[color]['kingside'] = False
        if fr == row and fc == 0: castling[color]['queenside'] = False

    apply_move_to_board(board, fr, fc, tr, tc, en_passant)
    state['en_passant'] = new_ep
    state['last_move'] = (fr, fc, tr, tc)

    opp = 'black' if color == 'white' else 'white'
    state['turn'] = opp

    in_check = is_in_check(board, opp)
    has_moves = any(legal_moves(state, r, c)
                    for r in range(8) for c in range(8)
                    if board[r][c] and board[r][c]['color'] == opp)
    if not has_moves:
        if in_check:
            state['phase'] = 'gameover'
            state['winner'] = color
        else:
            state['phase'] = 'gameover'
            state['winner'] = 'draw'
    state['check'] = opp if in_check else None
    return revealed

# ── AI Engine ─────────────────────────────────────────────────────────────────

AI_PIECE_VALUES = {'P': 100, 'N': 320, 'B': 330, 'R': 500, 'Q': 900, 'K': 20000}

AI_PST = {
    'P': [[ 0,  0,  0,  0,  0,  0,  0,  0],
          [50, 50, 50, 50, 50, 50, 50, 50],
          [10, 10, 20, 30, 30, 20, 10, 10],
          [ 5,  5, 10, 25, 25, 10,  5,  5],
          [ 0,  0,  0, 20, 20,  0,  0,  0],
          [ 5, -5,-10,  0,  0,-10, -5,  5],
          [ 5, 10, 10,-20,-20, 10, 10,  5],
          [ 0,  0,  0,  0,  0,  0,  0,  0]],
    'N': [[-50,-40,-30,-30,-30,-30,-40,-50],
          [-40,-20,  0,  0,  0,  0,-20,-40],
          [-30,  0, 10, 15, 15, 10,  0,-30],
          [-30,  5, 15, 20, 20, 15,  5,-30],
          [-30,  0, 15, 20, 20, 15,  0,-30],
          [-30,  5, 10, 15, 15, 10,  5,-30],
          [-40,-20,  0,  5,  5,  0,-20,-40],
          [-50,-40,-30,-30,-30,-30,-40,-50]],
    'B': [[-20,-10,-10,-10,-10,-10,-10,-20],
          [-10,  0,  0,  0,  0,  0,  0,-10],
          [-10,  0,  5, 10, 10,  5,  0,-10],
          [-10,  5,  5, 10, 10,  5,  5,-10],
          [-10,  0, 10, 10, 10, 10,  0,-10],
          [-10, 10, 10, 10, 10, 10, 10,-10],
          [-10,  5,  0,  0,  0,  0,  5,-10],
          [-20,-10,-10,-10,-10,-10,-10,-20]],
    'R': [[ 0,  0,  0,  0,  0,  0,  0,  0],
          [ 5, 10, 10, 10, 10, 10, 10,  5],
          [-5,  0,  0,  0,  0,  0,  0, -5],
          [-5,  0,  0,  0,  0,  0,  0, -5],
          [-5,  0,  0,  0,  0,  0,  0, -5],
          [-5,  0,  0,  0,  0,  0,  0, -5],
          [-5,  0,  0,  0,  0,  0,  0, -5],
          [ 0,  0,  0,  5,  5,  0,  0,  0]],
    'Q': [[-20,-10,-10, -5, -5,-10,-10,-20],
          [-10,  0,  0,  0,  0,  0,  0,-10],
          [-10,  0,  5,  5,  5,  5,  0,-10],
          [ -5,  0,  5,  5,  5,  5,  0, -5],
          [  0,  0,  5,  5,  5,  5,  0, -5],
          [-10,  5,  5,  5,  5,  5,  0,-10],
          [-10,  0,  5,  0,  0,  0,  0,-10],
          [-20,-10,-10, -5, -5,-10,-10,-20]],
    'K': [[-30,-40,-40,-50,-50,-40,-40,-30],
          [-30,-40,-40,-50,-50,-40,-40,-30],
          [-30,-40,-40,-50,-50,-40,-40,-30],
          [-30,-40,-40,-50,-50,-40,-40,-30],
          [-20,-30,-30,-40,-40,-30,-30,-20],
          [-10,-20,-20,-20,-20,-20,-20,-10],
          [ 20, 20,  0,  0,  0,  0, 20, 20],
          [ 20, 30, 10,  0,  0, 10, 30, 20]],
}

def ai_eval(board, color):
    score = 0
    for r in range(8):
        for c in range(8):
            p = board[r][c]
            if not p:
                continue
            ptype = 'Q' if p.get('hidden_queen') else p['type']
            val = AI_PIECE_VALUES.get(ptype, 0)
            pst_row = r if p['color'] == 'white' else 7 - r
            pst = AI_PST.get(ptype, [[0]*8]*8)
            pos_bonus = pst[pst_row][c]
            total = val + pos_bonus
            if p['color'] == color:
                score += total
            else:
                score -= total
    return score

def ai_negamax(state, depth, alpha, beta):
    color = state['turn']
    if state['phase'] == 'gameover':
        if state['winner'] == 'draw':
            return 0
        if state['winner'] != color:
            return -99000 + (3 - depth) * 100
        return 99000
    if depth == 0:
        return ai_eval(state['board'], color)

    best = float('-inf')
    board = state['board']
    any_move = False
    for r in range(8):
        for c in range(8):
            p = board[r][c]
            if not (p and p['color'] == color):
                continue
            for tr, tc in legal_moves(state, r, c):
                any_move = True
                s2 = copy.deepcopy(state)
                do_move(s2, r, c, tr, tc)
                val = -ai_negamax(s2, depth - 1, -beta, -alpha)
                if val > best:
                    best = val
                alpha = max(alpha, val)
                if alpha >= beta:
                    return best
    if not any_move:
        return -99000 if is_in_check(board, color) else 0
    return best

def ai_choose_move(state, ai_color, rating):
    all_moves = []
    board = state['board']
    for r in range(8):
        for c in range(8):
            p = board[r][c]
            if p and p['color'] == ai_color:
                for tr, tc in legal_moves(state, r, c):
                    all_moves.append((r, c, tr, tc))
    if not all_moves:
        return None

    if rating == 400:
        return random.choice(all_moves)

    blunder_rate = {800: 0.38, 1200: 0.12, 1600: 0.03}.get(rating, 0.0)
    depth        = {800: 1,    1200: 2,    1600: 3   }.get(rating, 3)

    if random.random() < blunder_rate:
        return random.choice(all_moves)

    scored = []
    for fr, fc, tr, tc in all_moves:
        s2 = copy.deepcopy(state)
        do_move(s2, fr, fc, tr, tc)
        val = -ai_negamax(s2, depth - 1, float('-inf'), float('inf'))
        scored.append((val, fr, fc, tr, tc))

    scored.sort(key=lambda x: -x[0])
    top_n = {1: 3, 2: 2, 3: 1}.get(depth, 1)
    candidates = scored[:top_n]
    _, fr, fc, tr, tc = random.choice(candidates)
    return fr, fc, tr, tc

def schedule_ai_move(room_id, delay=0.8):
    def _move():
        socketio.sleep(delay)
        if room_id not in rooms:
            return
        state = rooms[room_id]
        ai_color = state.get('ai_color')
        if not ai_color or state['phase'] != 'playing' or state['turn'] != ai_color:
            state['ai_thinking'] = False
            return
        if state['time_control'] and state['clock_turn_start']:
            elapsed = _time.time() - state['clock_turn_start']
            state['clocks'][ai_color] = max(0, state['clocks'][ai_color] - elapsed)
            if state['clocks'][ai_color] <= 0:
                state['phase'] = 'gameover'
                state['winner'] = 'timeout'
                state['loser'] = ai_color
                state['clock_turn_start'] = None
                state['ai_thinking'] = False
                _push_state(room_id)
                return
        move = ai_choose_move(state, ai_color, state.get('ai_rating', 1200))
        if not move:
            state['ai_thinking'] = False
            return
        fr, fc, tr, tc = move
        revealed = do_move(state, fr, fc, tr, tc)
        if state['time_control']:
            state['clock_turn_start'] = _time.time()
        if revealed:
            for sid in list(state['players'].keys()):
                socketio.emit('queen_revealed', {'color': ai_color, 'was_mine': False}, to=sid)
        state['ai_thinking'] = False   # FIX: clear flag before pushing state
        _push_state(room_id)
    socketio.start_background_task(_move)

# ── HTTP routes ───────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hidden Queen Chess</title>
<link href="https://fonts.googleapis.com/css2?family=Cinzel+Decorative:wght@700&family=Cinzel:wght@400;600&family=IM+Fell+English:ital@0;1&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
<style>
  :root {
    --ink: #1a1208;
    --parchment: #f5ead0;
    --gold: #c9993a;
    --gold-light: #e8bc5a;
    --light-sq: #f0d9a8;
    --dark-sq: #8b6942;
    --highlight: rgba(200,180,50,0.55);
    --move-dot: rgba(30,20,5,0.28);
    --board-size: min(78vw, 464px);
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body {
    font-family:'IM Fell English',serif;
    background:var(--ink); color:var(--parchment);
    min-height:100vh; display:flex; flex-direction:column; align-items:center;
    overflow-x:hidden;
  }
  body::before {
    content:''; position:fixed; inset:0;
    background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='200' height='200'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.75' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='200' height='200' filter='url(%23n)' opacity='0.04'/%3E%3C/svg%3E");
    pointer-events:none; z-index:9999;
  }
  header {
    text-align:center; padding:1.6rem 1rem .8rem;
    border-bottom:1px solid rgba(201,153,58,0.3); width:100%;
  }
  header h1 {
    font-family:'Cinzel Decorative',serif;
    font-size:clamp(1.3rem,4vw,2.2rem);
    color:var(--gold-light); text-shadow:0 2px 12px rgba(201,153,58,0.5);
  }
  header p { font-style:italic; color:rgba(245,234,208,0.55); font-size:.85rem; margin-top:.3rem; }

  /* ── Lobby ── */
  #lobby {
    display:flex; flex-direction:column; align-items:center;
    gap:1.4rem; padding:2.5rem 1rem; width:100%; max-width:480px;
  }
  .card {
    background:rgba(245,234,208,0.05); border:1px solid rgba(201,153,58,0.25);
    border-radius:4px; padding:1.6rem; width:100%; text-align:center;
  }
  .card h2 {
    font-family:'Cinzel',serif; color:var(--gold); font-size:1rem;
    margin-bottom:1rem; text-transform:uppercase; letter-spacing:.12em;
  }
  .btn {
    font-family:'Cinzel',serif; font-size:.82rem; letter-spacing:.1em;
    text-transform:uppercase; padding:.6rem 1.5rem;
    border:1px solid var(--gold); background:transparent;
    color:var(--gold-light); cursor:pointer; border-radius:2px; transition:all .2s;
  }
  .btn:hover { background:rgba(201,153,58,0.15); }
  .btn-primary { background:rgba(201,153,58,0.2); }
  .btn-primary:hover { background:rgba(201,153,58,0.35); }
  input[type=text] {
    font-family:'IM Fell English',serif; font-size:1rem;
    background:rgba(245,234,208,0.08); border:1px solid rgba(201,153,58,0.3);
    border-radius:2px; color:var(--parchment);
    padding:.5rem .8rem; width:100%; text-align:center; outline:none; margin-bottom:.8rem;
  }
  input[type=text]::placeholder { color:rgba(245,234,208,0.3); }
  input[type=text]:focus { border-color:var(--gold); }
  .room-id-display {
    font-family:'Cinzel',serif; font-size:1.3rem; color:var(--gold-light);
    letter-spacing:.2em; background:rgba(201,153,58,0.1);
    border:1px dashed rgba(201,153,58,0.4);
    padding:.6rem 1.2rem; border-radius:2px; cursor:pointer; transition:background .2s;
  }
  .room-id-display:hover { background:rgba(201,153,58,0.2); }
  .hint { font-size:.78rem; color:rgba(245,234,208,0.4); margin-top:.4rem; font-style:italic; }
  .time-options { display:flex; gap:.5rem; justify-content:center; flex-wrap:wrap; margin:.8rem 0 .4rem; }
  .time-btn {
    font-family:'Cinzel',serif; font-size:.75rem; padding:.35rem .7rem;
    border:1px solid rgba(201,153,58,0.35); background:transparent;
    color:rgba(245,234,208,0.6); cursor:pointer; border-radius:2px; transition:all .18s;
  }
  .time-btn:hover { border-color:var(--gold); color:var(--gold-light); }
  .time-btn.active { background:rgba(201,153,58,0.25); border-color:var(--gold); color:var(--gold-light); }
  .time-label { font-size:.78rem; color:rgba(245,234,208,0.45); margin-bottom:.4rem; }
  .rating-options { display:flex; gap:.5rem; justify-content:center; flex-wrap:wrap; margin:.8rem 0 .4rem; }
  .rating-btn {
    font-family:'Cinzel',serif; font-size:.72rem; padding:.35rem .75rem;
    border:1px solid rgba(201,153,58,0.35); background:transparent;
    color:rgba(245,234,208,0.6); cursor:pointer; border-radius:2px; transition:all .18s;
    white-space:nowrap;
  }
  .rating-btn:hover { border-color:var(--gold); color:var(--gold-light); }
  .rating-btn.active { background:rgba(201,153,58,0.25); border-color:var(--gold); color:var(--gold-light); }
  .ai-badge {
    display:inline-block; font-family:'Cinzel',serif; font-size:.62rem;
    letter-spacing:.1em; text-transform:uppercase;
    color:var(--gold); border:1px solid rgba(201,153,58,0.4);
    padding:.15rem .5rem; border-radius:2px; margin-bottom:.6rem;
  }

  /* ── Game area ── */
  #game { display:none; flex-direction:column; align-items:center; padding:.8rem; width:100%; }
  .clocks-row {
    display:flex; justify-content:space-between; align-items:center;
    width:var(--board-size); margin-bottom:.4rem;
  }
  .clock-box {
    font-family:'Cinzel',serif; font-size:1.1rem;
    padding:.3rem .8rem; border:1px solid rgba(201,153,58,0.2);
    border-radius:3px; background:rgba(201,153,58,0.05);
    color:rgba(245,234,208,0.6); transition:all .3s; min-width:80px; text-align:center;
  }
  .clock-box.active { color:var(--gold-light); border-color:rgba(201,153,58,0.5); background:rgba(201,153,58,0.12); }
  .clock-box.low    { color:#ff6b6b !important; border-color:rgba(255,107,107,0.5) !important; }
  .clock-box.unlimited { font-size:.7rem; color:rgba(245,234,208,0.35); letter-spacing:.05em; }
  .clock-label {
    font-family:'Cinzel',serif; font-size:.65rem; text-transform:uppercase;
    letter-spacing:.1em; color:rgba(245,234,208,0.35); display:block; margin-bottom:1px;
  }
  #status-bar {
    font-family:'Cinzel',serif; font-size:.82rem; letter-spacing:.08em;
    text-transform:uppercase; color:var(--gold); text-align:center;
    padding:.4rem 1.4rem; margin:.3rem 0;
    border:1px solid rgba(201,153,58,0.2); border-radius:2px;
    width:var(--board-size); background:rgba(201,153,58,0.06);
  }
  #status-bar.check   { color:#ff6b6b; border-color:rgba(255,107,107,0.4); }
  #status-bar.gameover { color:var(--gold-light); border-color:var(--gold); background:rgba(201,153,58,0.12); font-size:.95rem; }
  .board-outer { display:flex; flex-direction:column; align-items:flex-start; }
  .board-mid   { display:flex; align-items:stretch; }
  .rank-col {
    display:flex; flex-direction:column;
    width:18px; height:var(--board-size); justify-content:space-around; align-items:center;
    flex-shrink:0;
  }
  .rank-col span { font-family:'Cinzel',serif; font-size:.58rem; color:rgba(201,153,58,0.5); line-height:1; user-select:none; }
  .file-row { display:flex; width:var(--board-size); margin-left:18px; margin-top:3px; }
  .file-row span { flex:1; text-align:center; font-family:'Cinzel',serif; font-size:.58rem; color:rgba(201,153,58,0.5); user-select:none; }
  .board-wrap { border:3px solid var(--gold); box-shadow:0 0 0 1px rgba(201,153,58,0.3), 0 8px 40px rgba(0,0,0,0.7); flex-shrink:0; }
  #board { display:grid; grid-template-columns:repeat(8,1fr); width:var(--board-size); height:var(--board-size); }
  .sq { aspect-ratio:1; display:flex; align-items:center; justify-content:center; cursor:pointer; position:relative; transition:filter .1s; }
  .sq.light { background:var(--light-sq); }
  .sq.dark  { background:var(--dark-sq); }
  .sq.selected  { background:var(--highlight) !important; }
  .sq.last-move { background:rgba(180,160,40,0.35) !important; }
  .sq:hover { filter:brightness(1.08); }
  .move-dot::after { content:''; position:absolute; width:28%; height:28%; border-radius:50%; background:var(--move-dot); pointer-events:none; }
  .move-cap::after { content:''; position:absolute; inset:5%; border-radius:50%; border:3px solid var(--move-dot); pointer-events:none; }
  .piece { font-size: min(9vw, 52px); line-height:1; user-select:none; pointer-events:none; transition:transform .1s; }
  .piece.pc-white {
    color: #ffffff;
    text-shadow: 0 0 3px #000, 0 0 6px rgba(0,0,0,1), 1px 1px 0 #111, -1px -1px 0 #111, 1px -1px 0 #111, -1px 1px 0 #111;
  }
  .piece.pc-black {
    color: #0d0d0d;
    text-shadow: 0 0 3px #fff, 0 0 6px rgba(255,255,255,0.8), 1px 1px 0 rgba(255,255,255,0.5), -1px -1px 0 rgba(255,255,255,0.5), 1px -1px 0 rgba(255,255,255,0.5), -1px 1px 0 rgba(255,255,255,0.5);
  }
  .piece.hidden-q { filter: drop-shadow(0 0 6px rgba(255,200,50,1)) drop-shadow(0 0 3px rgba(201,153,58,0.9)); }

  /* ── Selection phase ── */
  #select-phase {
    display:none; flex-direction:column; align-items:center;
    gap:.7rem; padding:.8rem 1rem 1.5rem;
    text-align:center; width:100%; max-width:440px; overflow-y:auto;
  }
  #select-phase h2 { font-family:'Cinzel',serif; color:var(--gold); text-transform:uppercase; letter-spacing:.1em; font-size:1rem; }
  #select-phase .desc { font-style:italic; color:rgba(245,234,208,0.7); font-size:.82rem; line-height:1.5; }
  #select-board { display:grid; grid-template-columns:repeat(8,1fr); width:min(60vw,300px); height:min(60vw,300px); border:2px solid var(--gold); flex-shrink:0; }
  #select-board .sq { cursor:default; }
  #select-board .sq.pawn-row { cursor:pointer; }
  #select-board .sq.pawn-row:hover { filter:brightness(1.15); }
  #select-board .sq.sq-chosen { background:rgba(200,160,30,0.6) !important; box-shadow:inset 0 0 10px rgba(201,153,58,0.9); }
  #select-board .piece { font-size: min(7vw, 34px); }
  .select-controls { display:flex; align-items:center; justify-content:center; gap:1.2rem; flex-wrap:wrap; width:100%; }
  #confirm-btn { display:none; }
  #countdown-ring { position:relative; width:68px; height:68px; margin:0 auto; }
  #countdown-ring svg { transform:rotate(-90deg); }
  #countdown-ring circle { fill:none; stroke-width:4; stroke-dasharray:201; stroke-linecap:round; transition:stroke-dashoffset 1s linear,stroke .4s; }
  #countdown-track { stroke:rgba(201,153,58,0.15); }
  #countdown-arc   { stroke:var(--gold); stroke-dashoffset:0; }
  #countdown-num { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-family:'Cinzel',serif; font-size:1.2rem; color:var(--gold-light); font-weight:600; }
  #countdown-label { font-size:.7rem; color:rgba(245,234,208,.4); text-align:center; margin-top:.15rem; font-style:italic; }

  @keyframes revealQueen { 0% { transform:scale(0.4) rotate(-20deg); opacity:0; } 60% { transform:scale(1.25) rotate(4deg); } 100% { transform:scale(1) rotate(0); opacity:1; } }
  .piece.just-revealed { animation:revealQueen .5s ease-out forwards; }

  #reveal-banner { display:none; position:fixed; inset:0; z-index:2000; background:rgba(10,6,2,0.75); backdrop-filter:blur(4px); align-items:center; justify-content:center; }
  #reveal-banner.show { display:flex; }
  .reveal-card { background:linear-gradient(135deg,#1e1508,#2e1f0a); border:2px solid var(--gold); box-shadow:0 0 80px rgba(201,153,58,0.5), 0 0 20px rgba(201,153,58,0.2); border-radius:6px; padding:2.5rem 3.5rem; text-align:center; max-width:420px; }
  .reveal-card .crown { font-size:3rem; margin-bottom:.6rem; display:block; animation:revealQueen .6s ease-out; }
  .reveal-card h3 { font-family:'Cinzel Decorative',serif; color:var(--gold-light); font-size:1.4rem; margin-bottom:.5rem; letter-spacing:.05em; }
  .reveal-card p { font-style:italic; color:rgba(245,234,208,0.8); font-size:.95rem; margin-bottom:1.2rem; }
  .dismiss-btn { font-family:'Cinzel',serif; font-size:.78rem; letter-spacing:.12em; text-transform:uppercase; padding:.5rem 1.4rem; border:1px solid rgba(201,153,58,0.5); background:rgba(201,153,58,0.15); color:var(--gold); cursor:pointer; border-radius:2px; }
  .dismiss-btn:hover { background:rgba(201,153,58,0.3); }

  .player-tag { font-family:'Cinzel',serif; font-size:.75rem; letter-spacing:.1em; color:rgba(245,234,208,0.45); text-transform:uppercase; display:flex; align-items:center; gap:.5rem; }
  .dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
  .dot-white { background:#f0d9a8; }
  .dot-black { background:#2a2010; border:1px solid #8b6942; }
  .dot-you   { background:var(--gold); }
</style>
</head>
<body>

<header>
  <h1>⚜ Hidden Queen ⚜</h1>
  <p>A chess variant of deception and revelation</p>
</header>

<!-- LOBBY -->
<div id="lobby">
  <div class="card">
    <h2>Create Room</h2>
    <div class="time-label">Game Time per Player</div>
    <div class="time-options" id="time-options">
      <button class="time-btn" data-mins="3">3 min</button>
      <button class="time-btn" data-mins="5">5 min</button>
      <button class="time-btn active" data-mins="10">10 min</button>
      <button class="time-btn" data-mins="15">15 min</button>
      <button class="time-btn" data-mins="30">30 min</button>
      <button class="time-btn" data-mins="0">∞</button>
    </div>
    <button class="btn btn-primary" onclick="createRoom()">Create New Game</button>
    <div id="created-info" style="display:none;margin-top:1rem;">
      <p style="margin-bottom:.5rem;font-size:.82rem;color:rgba(245,234,208,.55)">Share this code:</p>
      <div class="room-id-display" id="room-code-display" onclick="copyCode()" title="Click to copy"></div>
      <p class="hint">Click to copy · Waiting for opponent…</p>
    </div>
  </div>
  <div class="card">
    <h2>Join Room</h2>
    <input type="text" id="join-input" placeholder="Enter room code" maxlength="6"
           style="text-transform:uppercase" oninput="this.value=this.value.toUpperCase()">
    <button class="btn" onclick="joinRoom()">Join Game</button>
  </div>
  <p id="lobby-msg" class="hint" style="color:#ff8a8a;min-height:1.2em;"></p>
</div>

<!-- AI CARD -->
<div id="lobby-ai" style="display:flex;flex-direction:column;align-items:center;width:100%;max-width:480px;padding:0 1rem 2rem;">
  <div class="card">
    <div class="ai-badge">⚔ vs Computer</div>
    <h2>Play vs AI</h2>
    <div class="time-label">AI Difficulty</div>
    <div class="rating-options" id="rating-options">
      <button class="rating-btn" data-rating="400">★☆☆☆ Beginner<br><small style="opacity:.6">400</small></button>
      <button class="rating-btn active" data-rating="800">★★☆☆ Casual<br><small style="opacity:.6">800</small></button>
      <button class="rating-btn" data-rating="1200">★★★☆ Skilled<br><small style="opacity:.6">1200</small></button>
      <button class="rating-btn" data-rating="1600">★★★★ Expert<br><small style="opacity:.6">1600</small></button>
    </div>
    <div class="time-label" style="margin-top:.6rem;">Game Time per Player</div>
    <div class="time-options" id="ai-time-options">
      <button class="time-btn" data-mins="3">3 min</button>
      <button class="time-btn active" data-mins="10">10 min</button>
      <button class="time-btn" data-mins="30">30 min</button>
      <button class="time-btn" data-mins="0">∞</button>
    </div>
    <button class="btn btn-primary" style="margin-top:.8rem;" onclick="playVsAI()">Play vs AI</button>
  </div>
</div>

<!-- SELECTION PHASE -->
<div id="select-phase">
  <h2>Choose Your Hidden Queen</h2>
  <p class="desc">Click one of your pawns below — it will secretly become a Queen.<br>
     Your opponent sees only a pawn until it strikes!</p>
  <div id="select-board"></div>
  <div class="select-controls">
    <div>
      <div id="countdown-ring">
        <svg width="68" height="68" viewBox="0 0 68 68">
          <circle cx="34" cy="34" r="30" id="countdown-track"/>
          <circle cx="34" cy="34" r="30" id="countdown-arc"/>
        </svg>
        <div id="countdown-num">15</div>
      </div>
      <div id="countdown-label">seconds to choose</div>
    </div>
    <button class="btn btn-primary" id="confirm-btn" onclick="confirmSelection()">Confirm Selection</button>
  </div>
  <p id="select-status" class="hint"></p>
</div>

<!-- GAME -->
<div id="game">
  <div style="display:flex;gap:1.5rem;align-items:center;margin-bottom:.4rem;">
    <div class="player-tag"><span class="dot dot-white"></span>White</div>
    <div class="player-tag"><span class="dot dot-black"></span>Black</div>
    <div class="player-tag" id="your-color-tag"></div>
    <div class="player-tag" id="ai-indicator" style="display:none;color:var(--gold);"></div>
  </div>
  <div class="clocks-row">
    <div id="clock-top" class="clock-box">
      <span class="clock-label" id="clock-top-label">black</span>
      <span id="clock-top-val">—</span>
    </div>
    <div id="status-bar">Loading…</div>
    <div id="clock-bottom" class="clock-box">
      <span class="clock-label" id="clock-bottom-label">white</span>
      <span id="clock-bottom-val">—</span>
    </div>
  </div>
  <div class="board-outer">
    <div class="board-mid">
      <div class="rank-col" id="rank-col"></div>
      <div class="board-wrap"><div id="board"></div></div>
    </div>
    <div class="file-row" id="file-row"></div>
  </div>
  <p class="hint" style="margin-top:.5rem;" id="game-hint"></p>
</div>

<!-- Reveal banner -->
<div id="reveal-banner">
  <div class="reveal-card">
    <span class="crown">👑</span>
    <h3 id="reveal-title">The Queen Reveals!</h3>
    <p id="reveal-msg">A hidden queen has made her move!</p>
    <button class="dismiss-btn" onclick="closeRevealBanner()">Continue</button>
  </div>
</div>

<script>
const socket = io();
let roomId = null, myColor = null;
let gameState = null;
let selected = null;
let legalMoves = [];
let hiddenQChoice = null;
let countdownInterval = null;
let countdownTotal = 15;
let selectedTimeMins = 10;
let selectedAiTimeMins = 10;
let selectedRating = 800;
let clockInterval = null;
let revealTimeout = null;
let isAiGame = false;

const PIECES = {
  white: { K:'♔', Q:'♕', R:'♖', B:'♗', N:'♘', P:'♙' },
  black: { K:'♚', Q:'♛', R:'♜', B:'♝', N:'♞', P:'♟' }
};

document.querySelectorAll('.time-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const parent = btn.closest('.time-options');
    parent.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    if (parent.id === 'ai-time-options') selectedAiTimeMins = parseInt(btn.dataset.mins);
    else selectedTimeMins = parseInt(btn.dataset.mins);
  });
});
document.querySelectorAll('.rating-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.rating-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    selectedRating = parseInt(btn.dataset.rating);
  });
});

function createRoom() { socket.emit('create_room', { time_mins: selectedTimeMins }); }
function joinRoom() {
  const code = document.getElementById('join-input').value.trim().toUpperCase();
  if (!code) return;
  socket.emit('join_room', { room_id: code });
}
function playVsAI() { socket.emit('play_vs_ai', { time_mins: selectedAiTimeMins, rating: selectedRating }); }
function copyCode() {
  const code = document.getElementById('room-code-display').textContent;
  navigator.clipboard.writeText(code).catch(()=>{});
  document.getElementById('room-code-display').textContent = '✓ Copied!';
  setTimeout(()=>{ document.getElementById('room-code-display').textContent = code; }, 1500);
}

socket.on('room_created', data => {
  roomId = data.room_id;
  document.getElementById('room-code-display').textContent = roomId;
  document.getElementById('created-info').style.display = 'block';
});
socket.on('error_msg', data => { document.getElementById('lobby-msg').textContent = data.msg; });
socket.on('game_start', data => {
  myColor = data.color; roomId = data.room_id;
  countdownTotal = data.timeout || 15; isAiGame = data.vs_ai || false;
  document.getElementById('lobby').style.display = 'none';
  document.getElementById('lobby-ai').style.display = 'none';
  showSelectionPhase(); startCountdown(countdownTotal);
  const aiInd = document.getElementById('ai-indicator');
  if (isAiGame) {
    const lbl = {400:'Beginner',800:'Casual',1200:'Skilled',1600:'Expert'};
    aiInd.textContent = `⚔ vs AI · ${lbl[data.ai_rating] || data.ai_rating}`;
    aiInd.style.display = 'flex';
  }
});
socket.on('opponent_selected', () => { document.getElementById('select-status').textContent = 'Opponent has chosen. Waiting for you…'; });
socket.on('waiting_for_selection', () => { document.getElementById('select-status').textContent = 'Selection confirmed. Waiting for opponent…'; });
socket.on('auto_selected', () => {
  stopCountdown();
  document.getElementById('select-status').textContent = '⏱ Time\'s up — a pawn was randomly chosen as your hidden queen!';
  document.getElementById('confirm-btn').style.display = 'none';
});

// FIX: clear selected piece and legal move highlights whenever a new game state
// arrives from the server. Previously these were never reset on incoming state,
// causing stale selection highlights and legal-move dots to persist after the
// opponent made a move.
socket.on('game_state', data => {
  gameState = data;
  if (data.phase === 'playing' || data.phase === 'gameover') {
    stopCountdown();
    document.getElementById('select-phase').style.display = 'none';
    document.getElementById('game').style.display = 'flex';
    // Clear stale selection state before re-rendering
    selected = null;
    legalMoves = [];
    renderBoard(data); updateStatus(data); updateClocks(data);
  }
});

socket.on('queen_revealed', data => { showRevealBanner(data.color, data.was_mine); });
socket.on('legal_moves', data => { legalMoves = data.moves; renderBoard(gameState); });

// FIX: notify the player when their opponent disconnects mid-game
socket.on('opponent_disconnected', () => {
  const bar = document.getElementById('status-bar');
  bar.className = 'gameover';
  bar.textContent = '🔌 Opponent disconnected';
  clearInterval(clockInterval);
});

function startCountdown(seconds) {
  const CIRC = 2 * Math.PI * 30;
  const arc = document.getElementById('countdown-arc');
  const num = document.getElementById('countdown-num');
  let remaining = seconds;
  arc.style.strokeDasharray = CIRC; arc.style.strokeDashoffset = '0'; num.textContent = remaining;
  countdownInterval = setInterval(() => {
    remaining = Math.max(0, remaining - 1); num.textContent = remaining;
    arc.style.strokeDashoffset = CIRC * (1 - remaining / seconds);
    const isLow = remaining <= 5;
    arc.style.stroke = isLow ? '#ff6b6b' : 'var(--gold)';
    num.style.color = isLow ? '#ff6b6b' : 'var(--gold-light)';
    if (remaining <= 0) stopCountdown();
  }, 1000);
}
function stopCountdown() {
  clearInterval(countdownInterval); countdownInterval = null;
  const arc = document.getElementById('countdown-arc');
  if (arc) { arc.style.strokeDashoffset = 2*Math.PI*30; arc.style.stroke = 'rgba(201,153,58,0.15)'; }
  const lbl = document.getElementById('countdown-label');
  if (lbl) lbl.textContent = '';
}

function showSelectionPhase() {
  document.getElementById('select-phase').style.display = 'flex';
  renderSelectBoard();
  document.getElementById('your-color-tag').innerHTML = `<span class="dot dot-you"></span>You are <strong>${myColor}</strong>`;
}
function renderSelectBoard() {
  const sb = document.getElementById('select-board'); sb.innerHTML = '';
  const pawnRow = myColor === 'white' ? 6 : 1;
  const rows = myColor === 'white' ? [0,1,2,3,4,5,6,7] : [7,6,5,4,3,2,1,0];
  rows.forEach(r => {
    for (let c = 0; c < 8; c++) {
      const sq = document.createElement('div');
      sq.className = 'sq ' + ((r+c)%2===0 ? 'light' : 'dark');
      if (r === pawnRow) {
        sq.classList.add('pawn-row'); sq.dataset.row = r; sq.dataset.col = c; sq.onclick = selectHiddenQueen;
        const span = document.createElement('span');
        span.className = 'piece pc-' + myColor; span.textContent = PIECES[myColor]['P'];
        sq.appendChild(span);
      }
      sb.appendChild(sq);
    }
  });
}
function selectHiddenQueen(e) {
  const sq = e.currentTarget;
  document.querySelectorAll('#select-board .pawn-row').forEach(s => {
    s.classList.remove('sq-chosen');
    const sp = s.querySelector('.piece');
    if (sp) { sp.textContent = PIECES[myColor]['P']; sp.classList.remove('hidden-q'); }
  });
  sq.classList.add('sq-chosen');
  const span = sq.querySelector('.piece');
  if (span) { span.textContent = PIECES[myColor]['Q']; span.classList.add('hidden-q'); }
  hiddenQChoice = { row: parseInt(sq.dataset.row), col: parseInt(sq.dataset.col) };
  document.getElementById('confirm-btn').style.display = 'inline-block';
}
function confirmSelection() {
  if (!hiddenQChoice) return;
  socket.emit('select_hidden_queen', { room_id: roomId, row: hiddenQChoice.row, col: hiddenQChoice.col });
  document.getElementById('confirm-btn').disabled = true;
  stopCountdown();
  document.getElementById('select-status').textContent = 'Selection confirmed. Waiting for opponent…';
}

function renderBoard(state) {
  const boardEl = document.getElementById('board'); boardEl.innerHTML = '';
  const board = state.board, lastMove = state.last_move;
  const rows = myColor === 'white' ? [0,1,2,3,4,5,6,7] : [7,6,5,4,3,2,1,0];
  const cols = myColor === 'white' ? [0,1,2,3,4,5,6,7] : [7,6,5,4,3,2,1,0];
  const rankCol = document.getElementById('rank-col'); rankCol.innerHTML = '';
  const fileRow = document.getElementById('file-row'); fileRow.innerHTML = '';
  (myColor === 'white' ? ['8','7','6','5','4','3','2','1'] : ['1','2','3','4','5','6','7','8'])
    .forEach(r => { const s = document.createElement('span'); s.textContent = r; rankCol.appendChild(s); });
  (myColor === 'white' ? ['a','b','c','d','e','f','g','h'] : ['h','g','f','e','d','c','b','a'])
    .forEach(f => { const s = document.createElement('span'); s.textContent = f; fileRow.appendChild(s); });
  rows.forEach(r => { cols.forEach(c => {
    const sq = document.createElement('div');
    sq.className = 'sq ' + ((r+c)%2===0 ? 'light' : 'dark');
    sq.dataset.row = r; sq.dataset.col = c;
    if (selected && selected.row===r && selected.col===c) sq.classList.add('selected');
    if (lastMove && ((r===lastMove[0]&&c===lastMove[1])||(r===lastMove[2]&&c===lastMove[3]))) sq.classList.add('last-move');
    const isLegal = legalMoves.some(m => m[0]===r && m[1]===c);
    if (isLegal) sq.classList.add(board[r][c] ? 'move-cap' : 'move-dot');
    const p = board[r][c];
    if (p) {
      const span = document.createElement('span');
      span.className = 'piece pc-' + p.color;
      const isOwnHiddenQ = p.color === myColor && p.hidden_queen && !state.revealed[myColor];
      if (isOwnHiddenQ) span.classList.add('hidden-q');
      const displayType = isOwnHiddenQ ? 'Q' : (p.type || 'P');
      span.textContent = PIECES[p.color][displayType] || PIECES[p.color]['P'];
      sq.appendChild(span);
    }
    sq.onclick = () => onSquareClick(r, c);
    boardEl.appendChild(sq);
  }); });
}

function onSquareClick(r, c) {
  if (!gameState || gameState.phase !== 'playing') return;
  if (gameState.turn !== myColor) return;
  const piece = gameState.board[r][c];
  if (selected) {
    if (legalMoves.some(m => m[0]===r && m[1]===c)) {
      socket.emit('make_move', { room_id: roomId, fr: selected.row, fc: selected.col, tr: r, tc: c });
      selected = null; legalMoves = []; return;
    }
    if (piece && piece.color === myColor) {
      selected = {row:r, col:c}; socket.emit('get_moves', { room_id: roomId, row: r, col: c });
      legalMoves = []; renderBoard(gameState); return;
    }
    selected = null; legalMoves = []; renderBoard(gameState); return;
  }
  if (piece && piece.color === myColor) {
    selected = {row:r, col:c}; socket.emit('get_moves', { room_id: roomId, row: r, col: c });
    legalMoves = []; renderBoard(gameState);
  }
}

function fmtSecs(s) {
  if (s === null || s === undefined) return '—';
  s = Math.max(0, Math.ceil(s));
  return Math.floor(s/60) + ':' + String(s%60).padStart(2,'0');
}
function updateClocks(state) {
  clearInterval(clockInterval);
  const unlimited = !state.clocks || (state.clocks.white === null);
  const oppColor = myColor === 'white' ? 'black' : 'white';
  document.getElementById('clock-top-label').textContent = oppColor;
  document.getElementById('clock-bottom-label').textContent = myColor;
  if (unlimited) {
    ['top','bottom'].forEach(side => {
      document.getElementById('clock-'+side).classList.add('unlimited');
      document.getElementById('clock-'+side+'-val').textContent = '∞';
    });
    return;
  }
  let clocks = { white: state.clocks.white, black: state.clocks.black };
  const startTs = state.clock_turn_start, activeTurn = state.turn;
  function refreshDisplay() {
    let wSecs = clocks.white, bSecs = clocks.black;
    if (startTs && state.phase === 'playing') {
      const elapsed = (Date.now()/1000) - startTs;
      if (activeTurn === 'white') wSecs = Math.max(0, wSecs - elapsed);
      else bSecs = Math.max(0, bSecs - elapsed);
    }
    const topSecs = myColor==='white' ? bSecs : wSecs;
    const bottomSecs = myColor==='white' ? wSecs : bSecs;
    const topBox = document.getElementById('clock-top'), bottomBox = document.getElementById('clock-bottom');
    topBox.classList.toggle('active', activeTurn===oppColor); topBox.classList.toggle('low', topSecs<30);
    bottomBox.classList.toggle('active', activeTurn===myColor); bottomBox.classList.toggle('low', bottomSecs<30);
    document.getElementById('clock-top-val').textContent = fmtSecs(topSecs);
    document.getElementById('clock-bottom-val').textContent = fmtSecs(bottomSecs);
  }
  refreshDisplay();
  if (state.phase === 'playing') clockInterval = setInterval(refreshDisplay, 500);
}

function updateStatus(state) {
  const bar = document.getElementById('status-bar'), hint = document.getElementById('game-hint');
  bar.className = '';
  if (state.phase === 'gameover') {
    bar.classList.add('gameover'); clearInterval(clockInterval);
    if (state.winner === 'draw') bar.textContent = '½–½  Stalemate — Draw!';
    else if (state.winner === myColor) bar.textContent = '♛  Victory!  You Win!';
    else if (state.winner === 'timeout') bar.textContent = state.loser === myColor ? '⏱ Time — You Lost' : '⏱ Opponent Flagged!';
    else bar.textContent = '♟  Defeat — Opponent Wins';
    hint.textContent = ''; return;
  }
  if (state.check) {
    bar.classList.add('check');
    bar.textContent = state.check === myColor ? '⚠ You are in Check!' : '⚠ Opponent in Check!';
  } else if (state.turn === myColor) {
    bar.textContent = '⚔  Your Turn';
    hint.textContent = state.revealed[myColor] ? '' : '♛ Your hidden queen glows gold — only you can see it';
  } else {
    bar.textContent = isAiGame ? '🤖  AI is thinking…' : '⏳  Opponent\'s Turn';
    hint.textContent = '';
  }
}

function showRevealBanner(color, wasMine) {
  clearTimeout(revealTimeout);
  document.getElementById('reveal-title').textContent = wasMine ? '👑 Your Queen Reveals!' : '👁 Enemy Queen Revealed!';
  document.getElementById('reveal-msg').textContent = wasMine
    ? 'Your hidden queen has made her move and is now known to all!'
    : 'Your opponent\'s hidden queen has struck from the shadows!';
  document.getElementById('reveal-banner').classList.add('show');
  revealTimeout = setTimeout(closeRevealBanner, 8000);
}
function closeRevealBanner() {
  clearTimeout(revealTimeout);
  document.getElementById('reveal-banner').classList.remove('show');
}
</script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML)

# ── Socket.IO handlers ────────────────────────────────────────────────────────

@socketio.on('create_room')
def on_create_room(data=None):
    data = data or {}
    time_mins = data.get('time_mins', 10)
    time_secs = int(time_mins) * 60 if time_mins and int(time_mins) > 0 else None
    room_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    init_room(room_id, time_secs)
    rooms[room_id]['players'][request.sid] = 'white'
    rooms[room_id]['sids']['white'] = request.sid
    join_room(room_id)
    emit('room_created', {'room_id': room_id})

@socketio.on('join_room')
def on_join_room(data):
    room_id = data.get('room_id','').upper()
    if room_id not in rooms:
        emit('error_msg', {'msg': 'Room not found.'}); return
    state = rooms[room_id]
    if len(state['players']) >= 2:
        emit('error_msg', {'msg': 'Room is full.'}); return
    state['players'][request.sid] = 'black'
    state['sids']['black'] = request.sid
    join_room(room_id)
    sids_list = list(state['players'].keys())
    colors = ['white', 'black']; random.shuffle(colors)
    state['players'] = {}; state['sids'] = {}
    for i, sid in enumerate(sids_list):
        state['players'][sid] = colors[i]; state['sids'][colors[i]] = sid
    for sid, color in state['players'].items():
        socketio.emit('game_start', {'color': color, 'room_id': room_id, 'timeout': SELECTION_TIMEOUT}, to=sid)
    state['phase'] = 'selecting'
    t = threading.Timer(SELECTION_TIMEOUT, selection_timer_fired, args=[room_id])
    t.daemon = True; state['selection_timer'] = t; t.start()

@socketio.on('select_hidden_queen')
def on_select_hidden_queen(data):
    room_id = data['room_id']
    if room_id not in rooms: return
    state = rooms[room_id]; color = state['players'].get(request.sid)
    if not color: return
    row, col = data['row'], data['col']
    board = state['board']; p = board[row][col]
    if not p or p['type'] != 'P' or p['color'] != color: return
    p['hidden_queen'] = True; state['selected'][color] = {'row': row, 'col': col}
    opp_sid = state['sids'].get('black' if color=='white' else 'white')
    if opp_sid: socketio.emit('opponent_selected', {}, to=opp_sid)
    emit('waiting_for_selection', {})
    if len(state['selected']) == 2:
        timer = state.pop('selection_timer', None)
        if timer: timer.cancel()
        broadcast_game_start(room_id)

@socketio.on('get_moves')
def on_get_moves(data):
    room_id = data['room_id']
    if room_id not in rooms: return
    state = rooms[room_id]; color = state['players'].get(request.sid)
    if not color or state['turn'] != color: return
    r, c = data['row'], data['col']
    emit('legal_moves', {'moves': legal_moves(state, r, c)})

@socketio.on('make_move')
def on_make_move(data):
    room_id = data['room_id']
    if room_id not in rooms: return
    state = rooms[room_id]; color = state['players'].get(request.sid)
    if not color or state['turn'] != color: return
    fr, fc, tr, tc = data['fr'], data['fc'], data['tr'], data['tc']
    if not (0 <= fr < 8 and 0 <= fc < 8 and 0 <= tr < 8 and 0 <= tc < 8): return
    board = state['board']; p = board[fr][fc]
    if not p or p['color'] != color: return
    moves = legal_moves(state, fr, fc)
    if (tr, tc) not in moves: return
    if state['time_control'] and state['clock_turn_start']:
        elapsed = _time.time() - state['clock_turn_start']
        state['clocks'][color] = max(0, state['clocks'][color] - elapsed)
        if state['clocks'][color] <= 0:
            state['phase'] = 'gameover'; state['winner'] = 'timeout'; state['loser'] = color
            state['clock_turn_start'] = None; _push_state(room_id); return
    revealed = do_move(state, fr, fc, tr, tc)
    if state['time_control']: state['clock_turn_start'] = _time.time()
    if revealed:
        for sid, c in state['players'].items():
            socketio.emit('queen_revealed', {'color': color, 'was_mine': c == color}, to=sid)
    _push_state(room_id)

@socketio.on('play_vs_ai')
def on_play_vs_ai(data):
    data = data or {}
    time_mins = data.get('time_mins', 10)
    ai_rating = int(data.get('rating', 1200))
    time_secs = int(time_mins) * 60 if time_mins and int(time_mins) > 0 else None
    room_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    init_room(room_id, time_secs)
    state = rooms[room_id]
    human_color = random.choice(['white', 'black'])
    ai_color = 'black' if human_color == 'white' else 'white'
    state['players'][request.sid] = human_color
    state['sids'][human_color] = request.sid
    state['ai_color'] = ai_color; state['ai_rating'] = ai_rating
    join_room(room_id)
    assign_random_queen(state, ai_color)
    state['phase'] = 'selecting'
    socketio.emit('game_start', {
        'color': human_color, 'room_id': room_id, 'timeout': SELECTION_TIMEOUT,
        'vs_ai': True, 'ai_rating': ai_rating,
    }, to=request.sid)
    t = threading.Timer(SELECTION_TIMEOUT, selection_timer_fired, args=[room_id])
    t.daemon = True; state['selection_timer'] = t; t.start()

# FIX: notify the remaining player when their opponent disconnects mid-game.
# Previously the opponent would see the game frozen with no feedback.
@socketio.on('disconnect')
def on_disconnect():
    for room_id, state in list(rooms.items()):
        if request.sid in state['players']:
            disconnected_color = state['players'][request.sid]
            del state['players'][request.sid]
            # Notify the other human player (if any) that the opponent left
            for remaining_sid in list(state['players'].keys()):
                socketio.emit('opponent_disconnected', {}, to=remaining_sid)
            if not state['players']:
                del rooms[room_id]
            break

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
