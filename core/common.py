# core/common.py
def format_time(seconds):
    if seconds is None:
        return "??:??"
    try:
        s = int(seconds)
        m, s = divmod(s, 60)
        return f"{m:02d}:{s:02d}"
    except Exception:
        return "??:??"

def find_closest_match(song_files, search_term):
    if not search_term:
        return None
    search_term = search_term.lower()
    best = None
    best_score = 0.0
    for s in song_files:
        lowered = s.lower()
        if search_term in lowered:
            score = len(search_term) / max(1, len(lowered))
            if score > best_score:
                best_score = score
                best = s
        for token in lowered.replace("_"," ").split():
            if token.startswith(search_term):
                score = len(search_term) / max(1, len(token))
                if score > best_score:
                    best_score = score
                    best = s
    return best
