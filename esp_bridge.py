#!/usr/bin/env python3
"""esp-bridge: Colab の VM と、学生の PC につないだ ESP32 を、ブラウザのタブ経由でつなぐ。

  ESP32 ⇄(USB / Web Serial)⇄ ブリッジのタブ(Chrome/Edge)⇄(Colab の proxyPort)⇄ このサーバー ⇄ esp コマンド

ノートブックで:
    import esp_bridge; esp_bridge.start()      # サーバー起動 + 「ブリッジを開く」リンク
ターミナル / AI エージェントから:
    esp flash [ファイル|プロジェクト]   書き込み(merged bin、または build/flash_args から自動で合成)
    esp log [-n 50] [-f] [--since-flash] シリアル出力を読む(/content/serial.log)
    esp send "f 200"                     ESP32 に文字を送る(既定で末尾に改行)
    esp reset                            ESP32 をリセット
    esp status                           ブリッジのタブがつながっているか

Python 標準ライブラリだけで動く。ESP-IDF の環境(export.sh)も不要。
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get('ESP_BRIDGE_PORT', '8765'))
LOG_PATH = os.environ.get('ESP_BRIDGE_LOG', '/content/serial.log' if os.path.isdir('/content') else os.path.join(HERE, 'serial.log'))
PAGE_TIMEOUT = 30          # この秒数ページからの問い合わせが無ければ「タブが閉じている」とみなす


# ---------------------------------------------------------------- merged bin の合成
def merge_flash_args(build_dir):
    """build/flash_args(idf.py build が出す)から 0x0 始まりの単一イメージを作る(esptool merge_bin 相当)。"""
    path = os.path.join(build_dir, 'flash_args')
    parts = []
    with open(path) as f:
        for line in f:
            tok = line.split()
            if len(tok) == 2 and tok[0].startswith('0x'):
                parts.append((int(tok[0], 16), os.path.join(build_dir, tok[1])))
    if not parts:
        raise SystemExit(f'{path} に書き込み対象がありません')
    end = max(addr + os.path.getsize(p) for addr, p in parts)
    image = bytearray(b'\xff' * end)
    for addr, p in parts:
        with open(p, 'rb') as f:
            data = f.read()
        image[addr:addr + len(data)] = data
    out = os.path.join(build_dir, 'esp-bridge-merged.bin')
    with open(out, 'wb') as f:
        f.write(image)
    return out


def resolve_firmware(arg):
    """引数(省略可)から書き込むファイルを決める。プロジェクトやビルドディレクトリなら合成する。"""
    target = os.path.abspath(arg or '.')
    if os.path.isfile(target):
        return target
    for d in (target, os.path.join(target, 'build')):
        if os.path.isfile(os.path.join(d, 'flash_args')):
            return merge_flash_args(d)
    raise SystemExit(f'書き込むものが見つかりません: {target}(.bin ファイルか、idf.py build 済みのプロジェクトを指定)')


# ---------------------------------------------------------------- サーバー
class State:
    def __init__(self):
        self.cv = threading.Condition()
        self.queue = deque()          # ページが取りに来る命令
        self.results = {}             # id -> {ok, msg}
        self.files = {}               # id -> 書き込むファイルのパス
        self.status = {}
        self.page_seen = 0.0
        self.seq = 0


S = State()


def log_append(lines):
    ts = time.strftime('%H:%M:%S')
    with open(LOG_PATH, 'a', encoding='utf-8', errors='replace') as f:
        for ln in lines:
            f.write(f'{ts} {ln}\n')


class Handler(BaseHTTPRequestHandler):
    server_version = 'esp-bridge'

    def log_message(self, *a):        # アクセスログは出さない
        pass

    def _send(self, code, body=b'', ctype='application/json'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(n) if n else b''

    def _q(self):
        from urllib.parse import parse_qs, urlparse
        u = urlparse(self.path)
        return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}

    def do_GET(self):
        path, q = self._q()
        if path in ('/', '/index.html'):
            with open(os.path.join(HERE, 'index.html'), 'rb') as f:
                return self._send(200, f.read(), 'text/html; charset=utf-8')
        if path == '/fw':
            p = S.files.get(q.get('id', ''))
            if not p or not os.path.isfile(p):
                return self._send(404, {'error': 'no firmware'})
            log_append([f'=== esp-bridge: flash {q.get("id")} 開始 {os.path.basename(p)} ==='])
            with open(p, 'rb') as f:
                return self._send(200, f.read(), 'application/octet-stream')
        if path == '/api/next':                 # ページ: 次の命令を待つ(ロングポーリング)
            deadline = time.time() + float(q.get('timeout', 20))
            with S.cv:
                S.page_seen = time.time()
                while not S.queue and time.time() < deadline:
                    S.cv.wait(deadline - time.time())
                    S.page_seen = time.time()
                cmd = S.queue.popleft() if S.queue else None
            return self._send(200, cmd or {})
        if path == '/api/result':               # CLI: 結果を待つ
            cid, deadline = q.get('id'), time.time() + float(q.get('timeout', 30))
            with S.cv:
                while cid not in S.results and time.time() < deadline:
                    S.cv.wait(deadline - time.time())
                r = S.results.get(cid)
            return self._send(200, r or {'pending': True})
        if path == '/api/status':
            alive = time.time() - S.page_seen < PAGE_TIMEOUT
            return self._send(200, {'page_alive': alive, 'page_seen_ago': round(time.time() - S.page_seen, 1)
                                    if S.page_seen else None, 'queued': len(S.queue), **S.status})
        return self._send(404, {'error': 'not found'})

    def do_POST(self):
        path, q = self._q()
        body = self._body()
        if path == '/api/log':                  # ページ: シリアルの行
            lines = body.decode('utf-8', 'replace').split('\n')
            log_append([ln for ln in lines if ln != ''])
            return self._send(200, {'ok': True})
        if path == '/api/status':               # ページ: 接続状態など
            S.status.update(json.loads(body or b'{}'))
            S.page_seen = time.time()
            return self._send(200, {'ok': True})
        if path == '/api/done':                 # ページ: 命令の結果
            r = json.loads(body or b'{}')
            with S.cv:
                S.results[str(r.get('id'))] = {'ok': bool(r.get('ok')), 'msg': r.get('msg', '')}
                S.cv.notify_all()
            if r.get('op') == 'flash':
                log_append([f'--- esp-bridge: flash {r.get("id")} {"成功" if r.get("ok") else "失敗"}: {r.get("msg", "")} ---'])
            return self._send(200, {'ok': True})
        if path == '/api/cmd':                  # CLI: 命令を積む
            cmd = json.loads(body or b'{}')
            with S.cv:
                S.seq += 1
                cid = f'{int(time.time())}-{S.seq}'
                cmd['id'] = cid
                if cmd.get('op') == 'flash':
                    S.files[cid] = cmd.pop('path')
                    cmd['size'] = os.path.getsize(S.files[cid])
                    cmd['name'] = os.path.basename(S.files[cid])
                S.queue.append(cmd)
                S.cv.notify_all()
            return self._send(200, {'id': cid})
        return self._send(404, {'error': 'not found'})


def serve(port):
    os.makedirs(os.path.dirname(LOG_PATH) or '.', exist_ok=True)
    srv = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    srv.daemon_threads = True
    print(f'esp-bridge: listening on {port}, log {LOG_PATH}', flush=True)
    srv.serve_forever()


# ---------------------------------------------------------------- CLI
def api(method, path, data=None, timeout=60):
    url = f'http://127.0.0.1:{PORT}{path}'
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b'{}')
    except urllib.error.URLError as e:
        raise SystemExit(f'esp-bridge のサーバーにつながりません({e})。ノートブックで esp_bridge.start() を実行してください。')


def need_page():
    st = api('GET', '/api/status')
    if not st.get('page_alive'):
        raise SystemExit('ブリッジのタブがつながっていません。esp_bridge.start() のリンクを開き、「ESP32 に接続」を押してください。')
    if not st.get('connected'):
        raise SystemExit('ブリッジのタブは開いていますが、ESP32 が未接続です。タブで「ESP32 に接続」を押してください。')
    return st


def run_cmd(cmd, wait):
    cid = api('POST', '/api/cmd', cmd)['id']
    deadline = time.time() + wait
    while time.time() < deadline:
        r = api('GET', f'/api/result?id={cid}&timeout=20', timeout=30)
        if not r.get('pending'):
            return r
    return {'ok': False, 'msg': f'{wait} 秒待っても終わりませんでした(タブで確認待ちかもしれません)'}


def tail(n, since_flash=False):
    if not os.path.exists(LOG_PATH):
        return []
    with open(LOG_PATH, encoding='utf-8', errors='replace') as f:
        lines = f.read().splitlines()
    if since_flash:
        idx = max((i for i, ln in enumerate(lines) if '=== esp-bridge: flash' in ln), default=-1)
        lines = lines[idx + 1:]
    return lines[-n:] if n else lines


def cli(argv):
    ap = argparse.ArgumentParser(prog='esp', description='ESP32 をブラウザ経由で操作する(esp-bridge)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('flash', help='書き込んでリセットする')
    p.add_argument('target', nargs='?', help='.bin ファイル、またはビルド済みプロジェクト(省略時はカレント)')
    p.add_argument('--wait-log', type=float, default=3, help='書き込み後、この秒数ぶんの出力を表示(既定 3)')
    p = sub.add_parser('log', help='シリアル出力を表示')
    p.add_argument('-n', type=int, default=50)
    p.add_argument('-f', '--follow', action='store_true')
    p.add_argument('--since-flash', action='store_true', help='最後の書き込み以降だけ')
    p = sub.add_parser('send', help='ESP32 に文字列を送る')
    p.add_argument('text')
    p.add_argument('--no-newline', action='store_true')
    sub.add_parser('reset', help='ESP32 をリセット')
    sub.add_parser('status', help='状態を表示')
    p = sub.add_parser('serve', help='サーバーを前面で起動(通常は esp_bridge.start() を使う)')
    p.add_argument('--port', type=int, default=PORT)
    a = ap.parse_args(argv)

    if a.cmd == 'serve':
        return serve(a.port)
    if a.cmd == 'status':
        print(json.dumps(api('GET', '/api/status'), ensure_ascii=False, indent=1))
        return
    if a.cmd == 'log':
        for ln in tail(a.n, a.since_flash):
            print(ln)
        if a.follow:
            pos = os.path.getsize(LOG_PATH) if os.path.exists(LOG_PATH) else 0
            while True:
                time.sleep(0.5)
                if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > pos:
                    with open(LOG_PATH, encoding='utf-8', errors='replace') as f:
                        f.seek(pos)
                        sys.stdout.write(f.read())
                        sys.stdout.flush()
                        pos = f.tell()
        return
    need_page()
    if a.cmd == 'flash':
        fw = resolve_firmware(a.target)
        print(f'書き込み: {fw}({os.path.getsize(fw)} バイト)… タブで確認が要る設定なら承認してください', flush=True)
        r = run_cmd({'op': 'flash', 'path': fw}, wait=300)
        print(('成功: ' if r['ok'] else '失敗: ') + r.get('msg', ''))
        if r['ok'] and a.wait_log > 0:
            time.sleep(a.wait_log)
            print('--- 書き込み後の出力 ---')
            for ln in tail(40, since_flash=True):
                print(ln)
        sys.exit(0 if r['ok'] else 1)
    if a.cmd == 'send':
        r = run_cmd({'op': 'send', 'text': a.text + ('' if a.no_newline else '\n')}, wait=30)
    elif a.cmd == 'reset':
        r = run_cmd({'op': 'reset'}, wait=30)
    print(('成功: ' if r['ok'] else '失敗: ') + r.get('msg', ''))
    sys.exit(0 if r['ok'] else 1)


# ---------------------------------------------------------------- ノートブック用
def _running(port):
    """True: esp-bridge が動いている / False: 何も動いていない / それ以外のものが使っていれば例外。"""
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status', timeout=2) as r:
            if 'page_alive' in json.loads(r.read() or b'{}'):
                return True
    except urllib.error.URLError as e:
        if not isinstance(e, urllib.error.HTTPError):
            return False
    except (OSError, ValueError):
        return False
    raise SystemExit(f'ポート {port} は esp-bridge 以外のプログラムが使っています。止めるか、start(port=別の番号) にしてください。')


def start(port=PORT, install_cli=True):
    """サーバーを(まだなら)裏で起動し、ブリッジを開くリンクを出す。ノートブックのセルから呼ぶ。"""
    if not _running(port):
        out = open(os.path.join(HERE, 'server.log'), 'a')
        subprocess.Popen([sys.executable, os.path.abspath(__file__), 'serve', '--port', str(port)],
                         stdout=out, stderr=out, start_new_session=True,
                         env={**os.environ, 'ESP_BRIDGE_PORT': str(port)})
        for _ in range(50):
            if _running(port):
                break
            time.sleep(0.1)
    if install_cli:
        wrapper = '/usr/local/bin/esp'
        try:
            with open(wrapper, 'w') as f:
                f.write(f'#!/bin/sh\nESP_BRIDGE_PORT={port} exec {sys.executable} {os.path.abspath(__file__)} "$@"\n')
            os.chmod(wrapper, 0o755)
        except OSError as e:
            print(f'esp コマンドを入れられませんでした({e})。python3 {os.path.abspath(__file__)} … で代用してください。')
    try:
        from google.colab import output
        from IPython.display import HTML, display
        url = output.eval_js(f'google.colab.kernel.proxyPort({port})')
        # iframe ではシリアル通信が禁止されるので、必ず別タブで開く
        display(HTML(f'<p><a href="{url}" target="_blank" rel="noopener" style="font-size:18px">▶ ESP32 ブリッジを開く(新しいタブ)</a>'
                     f'<br><small>開かない場合はこの URL を Chrome / Edge で開く: {url}</small></p>'))
    except ImportError:
        print(f'Colab ではありません。http://localhost:{port}/ を Chrome で開いてください。')


if __name__ == '__main__':
    cli(sys.argv[1:])
