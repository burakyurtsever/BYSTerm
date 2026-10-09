#!/usr/bin/env python3
"""ci/i18n_map.json -> src/bysterm_i18n_data.py (uygulamanin icine gomulen ceviri sozlugu).

i18n_map.json: {"kaynak metin": {"en": "...", "tr": "..."}}   (kaynak = koddaki metin, birebir)
FULL : tam metin eslesmesi.   FRAGS: bicimli (f-string) metinlerin sabit parcalari, uzundan kisaya.
Kullanim: python3 ci/gen_i18n.py
"""
import os
import re
import json
import pprint

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
SRC = os.path.join(ROOT, 'ci', 'i18n_map.json')
FRAGS_LIST = os.path.join(ROOT, 'ci', 'i18n_frags.json')     # hangi kaynaklar f-string parcasi
OUT = os.path.join(ROOT, 'src', 'bysterm_i18n_data.py')


def main():
    m = json.load(open(SRC, encoding='utf-8'))
    frag_keys = set(json.load(open(FRAGS_LIST, encoding='utf-8'))) if os.path.exists(FRAGS_LIST) else set()
    full, frags = {}, []
    for src, v in m.items():
        en, trk = v.get('en', src), v.get('tr', src)
        if not isinstance(en, str) or not isinstance(trk, str):
            continue
        full[src] = (en, trk)
        if src in frag_keys and len(src.strip()) >= 4 and re.search(r'[A-Za-z]{3}', src):
            # cok kisa/harfsiz parcalar baska metinleri bozabilir -> alma
            frags.append((src, en, trk))
    frags.sort(key=lambda t: -len(t[0]))
    with open(OUT, 'w', encoding='utf-8') as f:
        f.write('# Uretilmis dosya (ci/gen_i18n.py). Elle duzenlemeyin; ci/i18n_map.json duzenleyip yeniden uretin.\n')
        f.write('# -*- coding: utf-8 -*-\n')
        f.write('FULL = ' + pprint.pformat(full, width=120) + '\n')
        f.write('FRAGS = ' + pprint.pformat(frags, width=120) + '\n')
    print(f'{len(full)} tam, {len(frags)} parca -> {OUT}')


if __name__ == '__main__':
    main()
