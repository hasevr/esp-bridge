# esp-bridge — Colab から手元の ESP32 に書き込み・シリアル通信する

Colab の VM はクラウドにあるので、手元の PC につないだ ESP32 には直接届かない。
esp-bridge は、PC の Chrome / Edge で開いた「ブリッジのタブ」を中継にして、
Colab のターミナルや AI エージェントから ESP32 を操作できるようにする。

```
ESP32 ⇄ USB(Web Serial)⇄ ブリッジのタブ(Chrome/Edge)⇄ Colab の proxyPort ⇄ bridge サーバー ⇄ esp コマンド
```

- ノートブックの中(出力欄)ではブラウザがシリアル通信を禁止しているので、ブリッジは**必ず別タブ**で開く。
- ブリッジのタブの URL は Colab の proxyPort で、本人のブラウザ(Google ログイン)からしか開けない。
- 書き込みはブラウザ上の [esptool-js](https://github.com/espressif/esptool-js)(Espressif 公式)で行う。

## 使い方(学生)

1. ノートブックのセルで起動する。
   ```python
   !git clone -q https://github.com/hasevr/esp-bridge /content/esp-bridge 2>/dev/null || true
   import sys; sys.path.insert(0, '/content/esp-bridge')
   import esp_bridge; esp_bridge.start()
   ```
2. 出てきた「▶ ESP32 ブリッジを開く」を開く(Chrome か Edge)。
3. ESP32 を USB でつなぎ、タブの「ESP32 に接続」を押してポートを選ぶ(初回だけ)。
   緑の「ESP32: 接続中」になれば準備完了。タブは閉じずに置いておく。
4. Colab のターミナル(または AI エージェント)から:
   ```sh
   cd /content/ActiveHaptic && idf.py build
   esp flash              # ビルド済みのプロジェクトをそのまま書き込む(merged bin を自動で作る)
   esp log -n 30          # シリアル出力の直近30行
   esp log -f             # 出力を流し続ける(Ctrl+C で止める)
   esp send "f 200"       # ESP32 に文字を送る(キーボード入力の代わり)
   esp reset              # リセット
   esp status             # タブ・ESP32 の接続状態
   ```
   シリアル出力は `/content/serial.log` にも時刻付きで溜まる。

**書き込み前に確認する**(タブのチェックボックス、既定で ON): `esp flash` のたびにタブに確認が出て、
人が「書き込む」を押すまで待つ。モータが急に動いても安全な状態かを人が確かめるため。

## AI エージェントに渡す説明(Custom Instructions / AGENTS.md に貼る)

```
ESP32 は手元の PC に USB でつながっており、Colab からは esp-bridge 経由で操作する。
- 書き込み: プロジェクトで `idf.py build` した後 `esp flash`(人がブラウザで承認するまで待つ)。
- 出力の確認: `esp log -n 50` / 書き込み後だけ見るなら `esp log --since-flash`。
- 入力: `esp send "<文字列>"`、リセット: `esp reset`。
- `esp status` で page_alive / connected が false なら、人にブリッジのタブを開いて接続するよう頼む。
- idf.py flash / idf.py monitor は使えない(Colab に USB は無い)。
```

## 試し方(実機なし)

ブリッジのタブの URL の末尾に `?fake=1` を付けると、偽の ESP32(1秒ごとに ADC 値を出し、
送った文字をエコーする)につながる。書き込みは「ファイルを受け取ったところまで」を確かめる。

## 制限

- Chrome / Edge のみ(Safari・Firefox は Web Serial 非対応)。USB シリアルのドライバ(CP210x)は従来どおり必要。
- ブリッジのタブと Colab を開いたままにしておく必要がある。Colab の VM が切れたら `esp_bridge.start()` からやり直し
  (VM が変わるとタブの URL も変わり、ポートの選択も1回やり直しになる)。
- 書き込みが「Failed to connect」で失敗するときは、ボードの BOOT ボタンを押しながら再実行する。
- `esp flash` に渡せるのは 0x0 から書く単一イメージ(merged bin)か、`idf.py build` 済みのプロジェクト。

## ファイル

- `esp_bridge.py` — サーバー・`esp` コマンド・`start()`(Python 標準ライブラリのみ)
- `index.html` — ブリッジのタブ(esptool-js 0.7.0 を jsDelivr から読み込む)
