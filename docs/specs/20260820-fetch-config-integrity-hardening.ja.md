# フェッチ・設定・復元の境界強化

ステータス: 確定

## 最終案

ChatGPTによる外部レビューで指摘された5点(SSRF防御のDNS rebinding、fetch設定の配線漏れ、restore検証の甘さ、設定パースのbool緩さ、ElementTree警告)を是正する。レビュー1〜5(Claude Code 3回・ChatGPT 2回)を経て、草案からA節とB節が大きく変わった。C・D・E節は草案のまま採用する。

### A. SSRF防御のDNS rebinding対策

**閉じる範囲を明確にする。** 目的は「urllibを用いるdirect fetch経路(page fetchとRSS fetchの両方)ではDNS検証と実接続を一致させ、DNS rebindingによるTOCTOUを閉じる」ことに限定する。Browser fallback(Playwright/Chromium)については、恒久的な検証キャッシュを廃止し各requestを再検証するところまでを行うが、Chromium内部の名前解決自体との間のTOCTOUは残存リスクとして受容する(下記「受け入れる不正確さ」)。

**方式は「IPを上位層へ渡す」のではなく、接続クラスの`_create_connection`を差し替える。** `http.client.HTTPConnection.connect()`は`self._create_connection((self.host, self.port), self.timeout, self.source_address)`という差し替え可能なインスタンス属性経由で接続しており、`HTTPSConnection.connect()`も`super().connect()`を通じてこれを使う。`connect()`自体は上書きしない。

```python
def _validated_create_connection(address, timeout, source_address, *, allowed_private_hosts):
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if host.lower() not in allowed_private_hosts:
        for *_, sockaddr in infos:
            candidate = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
            if not candidate.is_global:
                raise ValueError(f"non-public address is not allowed: {candidate}")
    last_error: Exception | None = None
    for family, socktype, proto, _, sockaddr in infos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            if sock is not None:
                sock.close()
    raise last_error or OSError("getaddrinfo returned an empty list")
```

`socket.create_connection`は呼ばない。それ自体が内部で`getaddrinfo`を再実行するため(渡す`host`が数値IPであってもgetaddrinfo自体は再実行される)、最初の1回の`getaddrinfo`が返した`family`/`socktype`/`proto`/`sockaddr`をそのまま使って`socket.socket(...)`を生成し`connect(sockaddr)`する。`sockaddr`を`(host, port)`へ切り詰めない。IPv6の`sockaddr`は`(address, port, flowinfo, scope_id)`の4要素であり、切り詰めると`flowinfo`/`scope_id`を失う。

検証は`getaddrinfo`が返した**全アドレス**に対して行い、1つでも非globalなら全体を拒否する(`allowed_private_hosts`に該当する場合を除く)。接続は検証済みaddrinfo集合の中だけで試み、hostnameを再解決しない。単一IPに絞る設計は採らない。

`_ValidatingHTTPConnection`/`_ValidatingHTTPSConnection`は`HTTPConnection`/`HTTPSConnection`をそれぞれ継承し、`__init__`で`self._create_connection`を上記関数へ差し替えるだけの薄いラッパーとする。`HTTPSConnection`側のTLS wrap・`_tunnel_host`処理は標準の`connect()`実装のまま変更しない。

**リダイレクトは追加の実装を要しない。** `SafeRedirectHandler`(`feedian/extract.py:115-122`)は検証済みURLへ`redirect_request`するだけであり、実際の接続はホップごとに新しい`Request`から新しい接続インスタンスを通るため、ホップごとに独立して解決・検証・接続される。

**page fetchとRSS fetchは共通のtransportを共有するが、共有するのは接続の安全性設定に限る。** `feedian/rss.py`の`fetch_rss_items`(独自の`build_opener(HTTPSHandler(...), SafeRedirectHandler(...))`を持つ)も同じ`_ValidatingHTTPConnection`/`_ValidatingHTTPSConnection`経由のopenerを使う。共通化する設定は`NetworkPolicy`(下記B節)に限り、RSS固有の`timeout_seconds`(既定30秒)や`FEED_XML_MAX_BYTES`(10 MiB)はRSS側の値のまま変更しない。`fetch.timeout_seconds`(既定5秒)・`fetch.document_max_bytes`とは意味が異なる別概念であるため、`FetchPolicy`を丸ごと渡さない。

**平文HTTPとproxyも対象にする。** `build_opener`へ`_ValidatingHTTPConnection`を使う`HTTPHandler`相当のハンドラも明示的に渡す(既定の`HTTPHandler`を暗黙に使わせない)。あわせて`ProxyHandler({})`を明示し、`HTTP_PROXY`/`HTTPS_PROXY`等の環境変数によるproxy自動利用を止める。Feedianはproxy対応をこれまで文書化しておらず、`build_opener`が既定ハンドラを暗黙に追加する副作用に過ぎなかった。今回のSSRF保証(検証したアドレスに実際に接続する)を成立させるため、Feedian自身のHTTP/RSS fetch transportに限りproxyを非対応とする。ローカルLLM agentへのproxy環境変数の受け渡しとは別問題であり、変更しない。

**`_create_connection`はCPythonの内部属性であり公開APIではない。** Feedianが対象とするPython 3.11/3.12/3.13それぞれで、`HTTPConnection.connect()`が`self._create_connection(...)`経由で接続し、`HTTPSConnection.connect()`が`super().connect()`後にTLS wrapする構造を持つことをテストで固定する。差し替え関数のシグネチャは`(address, timeout, source_address)`の3引数を標準の`socket.create_connection`と互換に保つ。

**Browser fallback。** `_validated_browser_hosts`による恒久キャッシュ(`feedian/extract.py:874`付近)を廃止し、`page.route()`のたびに`validate_fetch_url`を必ず呼ぶ。`BrowserCandidate`(`feedian/extract.py:74-83`)の`allow_private_urls: bool`フィールドは、単体のフラグではなく`policy: FetchPolicy`を保持する形に変える。ワーカースレッドからmainスレッドへの引き継ぎ(`complete_browser_fallback`)で設定が構造的に揃う。

### B. NetworkPolicy / FetchPolicyの導入とconfig配線

責務を2段に分ける。「1回のfetchをどう安全に行うか」を`NetworkPolicy`、「page fetch固有のサイズ・timeout」を`FetchPolicy`とする。

```python
@dataclass(frozen=True)
class NetworkPolicy:
    allowed_private_hosts: frozenset[str]

@dataclass(frozen=True)
class FetchPolicy:
    network: NetworkPolicy
    html_max_bytes: int
    document_max_bytes: int
    timeout_seconds: int
    browser_timeout_seconds: int
```

`feedian/vault.py`に`fetch_policy(config: VaultConfig) -> FetchPolicy`を追加し、既存の`fetch_retry_settings`と同じ場所で検証する。`html_max_bytes`/`document_max_bytes`/`timeout_seconds`/`browser_timeout_seconds`は`positive_int_setting`で検証する(bool拒否・1以上)。`fetch_page_text`・`render_html_with_browser`・`fetch_page_text_with_browser`のシグネチャは、個別引数の羅列ではなく`policy: FetchPolicy`を受け取る形に変える。`fetch_rss_items`は`network: NetworkPolicy`だけを受け取り、自身の`timeout_seconds`(既定30)・`FEED_XML_MAX_BYTES`はそのまま維持する。`sync.py`の呼び出し箇所はconfigから`fetch_policy(config)`を1回構築し、全呼び出しへ同じインスタンスを渡す。`allow_private_urls=False`の直書き(`sync.py:390`、`sync.py:696`)は除去する。

**`allow_private_hosts`の入力検証。** `config.fetch["allow_private_hosts"]`はJSON array of stringのみ許可する。要素はtrimして小文字化し、空文字は拒否、重複は除去する(`terminal_failure_kinds`(`feedian/vault.py:529-531`)と同じ形の検証)。文字列1個をそのまま渡すような入力(例: `"allow_private_hosts": "localhost"`)は拒否し、`frozenset(value)`のような文字ごとの分解を起こさせない。

**判定方式。** `VaultConfig`(`sync.py`・RSS fetch)の経路では`allow_private_urls: bool`という全面許可フラグは残さない。検証関数は`allowed_private_hosts: frozenset[str]`を受け取り、**解決前のhostname文字列(正規化済み)がこの集合に含まれる場合のみ**、privateアドレスチェックをその1ホストに限りスキップする。1ホストを許可しても他の全private IPを許可しないよう、集合要素ごとの一致で判定する。

**legacy `Config`(`feedian/config.py`)経路は対象外とし、全面フラグを維持する。** `feedian/__main__.py`はCOMMANDSに載らない別のCLIモード(`--source hatena`等)を持ち、`feedian/config.py`の`Config`(`VaultConfig`とは別のdataclass)を使って`fetch_page_text`を4箇所(`__main__.py:639,843,900,1340`)から直接呼んでいる。`Config.allow_private_urls: bool`はhost単位ではない全面許可フラグであり、`NetworkPolicy`の設計とは相容れない。この経路専用に、`Config`から`FetchPolicy`を組み立てる小さなアダプタ(`allow_private_urls=True`なら全アドレスを許可する`NetworkPolicy`相当の扱いとする)を設け、既存のCLI挙動を変えない。新しい`VaultConfig.fetch["allow_private_hosts"]`の設計をこのアダプタへ拡張することはしない。詳細は改訂2を参照。

### C. restore検証をGit tagの値へ固定する

**脅威モデル。** 本検証は、Git repositoryのcommit/tag情報は信頼でき、GitHub Release archiveのみが破損・改ざんし得るという前提に立つ(trusted Git repositoryをarchive外の信頼点とする)。Git repository自体が攻撃者に書き換えられた場合の真正性は保証しない。tag名は書き換え可能な参照であり、署名付きtagでもない。

**責務を2層に分ける。**

- `restore_database(vault_root, archive, tag)` — `tag`を必須引数とする。ローカルGitに当該tagが既に存在することを前提とし(fail-fast)、`git show <tag>:.feedian/snapshot.json`を読んでarchiveのsha256を照合してから展開する。
- `download_and_restore(vault_root, tag)` — 既に`tag`を受け取っている(`feedian/restore.py:50`)。GitHub Releaseからarchiveをダウンロードする前後のいずれかで、originから当該tagをfetchしてから`restore_database`を呼ぶ(fresh clone直後や、別マシンで作成された新しいtagがローカルに存在しないケースに対応する)。

検証手順は次の順序で行い、一致しなければ展開しない。

```
1. git show <tag>:.feedian/snapshot.json を読み、archive.sha256を取り出す
2. 指定されたarchiveファイルのsha256を計算し、1の値と一致することを確認する
3. 一致しなければ即座に失敗する
4. (従来通り) 7z test → 展開 → manifest.json内のdatabase.sha256と照合 → PRAGMA integrity_check
```

既存の「archiveファイルだけ渡せば復元できる(tagなし)」経路は廃止する。破壊的変更であり、Git管理下のVaultでない、またはタグが存在しない場合は復元できない。

### D. 設定パースの`bool`厳密化

`feedian/vault.py`に`strict_bool_setting(name: str, value: object) -> bool`を追加し、`positive_int_setting`と同じ形で`bool`以外を`ValueError`にする。

```python
def strict_bool_setting(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false, got {value!r}")
    return value
```

`_parse_providers`(`ProviderSettings.enabled`、`feedian/vault.py:411`)と`_parse_rss_feed`(`RssFeedSettings.enabled`、`:474`)の`enabled`変換をこれに置き換える。デフォルト値(`True`)は`value.get("enabled", True)`のまま維持し、キーが実際に存在する場合のみ型を検証する。対象は現行Vault configのこの2箇所のboolean設定であり、「Feedian全体」ではない。

### E. ElementTree truth-testing警告の解消

`feedian/rss.py:113`を明示的な`is None`判定に置き換える。

```python
channel = root if atom else _first_descendant(root, "channel")
if channel is None:
    channel = root
```

### 検証

```
python -m pytest -q
```

**A. SSRF/DNS rebinding**

1. `_ValidatingHTTPConnection`/`_ValidatingHTTPSConnection`が、`getaddrinfo`の全アドレスを検証してから、その集合の中だけで接続する。1つでも非globalなアドレスがあれば全体を拒否する。
2. `_validated_create_connection`内の`socket.getaddrinfo`は1回だけ呼ばれ、取得した`family`/`socktype`/`proto`/`sockaddr`を直接`socket.socket(...).connect(sockaddr)`へ使用する。`socket.create_connection`は呼ばない。
3. Host/SNIは検証済みアドレスではなく元のhostnameのまま送られる。
4. リダイレクト先ごとに独立して解決・検証・接続が行われる。
5. 平文`http://`のリクエストも検証済み接続を経由する。
6. proxy環境変数(`HTTP_PROXY`/`HTTPS_PROXY`)を設定してもproxyへ接続しない。
7. `fetch_rss_items`も同じ検証済み接続を経由するが、`timeout_seconds`は30秒のまま、`FEED_XML_MAX_BYTES`は10 MiBのまま変わらない。
8. Browser fallbackで同一hostへの2回目のrequestでも`validate_fetch_url`が毎回呼ばれる(恒久キャッシュが存在しないことを確認)。
9. `BrowserCandidate`が`policy: FetchPolicy`を保持する。
10. 対象Python 3.11/3.12/3.13それぞれで`_create_connection`差し替えが機能する。

**B. NetworkPolicy / FetchPolicy**

11. `config.fetch["html_max_bytes"]`を変更すると`fetch_page_text`が実際にその上限で打ち切る。
12. `config.fetch["document_max_bytes"]`も同様。
13. `allow_private_hosts`に1ホストだけ指定すると、そのホストのみprivateアドレスチェックをスキップし、他のprivateホストは従来通り拒否される。
14. `allow_private_hosts`が配列以外(文字列単体・数値など)なら`ValueError`になる。要素はtrim・小文字化・重複除去される。
15. `sync.py`の呼び出し箇所に`allow_private_urls=False`のハードコードが残っていない。

**C. restore検証**

16. archiveのsha256がGit tag側の`.feedian/snapshot.json`と一致しない場合、展開前に失敗する。
17. 一致する場合は従来通りDB sha256照合とintegrity_checkまで進む。
18. ローカルにtagが存在しない場合、`restore_database`は復元できない。
19. `download_and_restore`はローカルにtagがなくてもoriginからfetchしてから復元できる。

**D. bool厳密化**

20. `"enabled": "false"`(文字列)や`"enabled": 1`(整数)を渡すと`ValueError`になる。
21. `"enabled": true`/`"enabled": false`(JSON真偽値)は従来通り動作する。

**E. warning解消**

22. RSS関連のテストで`xml.etree.ElementTree`由来のDeprecationWarningが出なくなる。

### 受け入れる不正確さ

- **Browser fallback(Chromium)の実接続DNSとのTOCTOUは閉じない。** Chromiumは`--host-resolver-rules`という起動引数でホスト名解決を上書きできるが、これは`launch()`時に固定されるプロセス全体設定であり、`_browser`をモジュールレベルの単一インスタンスとして使い回す現行構造ではrequest単位に適用できない。恒久キャッシュの除去(requestごとの再検証)までを行い、これ以上は追わない。
- **proxy経由の接続はFeedian自身のHTTP/RSS fetch transportで一律非対応にする。** 文書化された機能ではなく、`build_opener`の暗黙の副作用だったため実質的な機能損失はないと判断する。proxy対応が必要になった場合は、明示的なtrusted proxy設定を導入する別仕様とする。
- **`_create_connection`はCPythonの内部属性であり、将来のPythonバージョンで構造が変わる可能性がある。** 対象バージョン(3.11/3.12/3.13)でのテストが通っている限り採用し、新しいバージョンをサポート対象に加える際は同じテストで再確認する。

## 改訂

### 改訂1 — Claude Code (2026-08-20)

**該当箇所:** A節の`_validated_create_connection`のコード例、および検証項目2。

**(前)**

```python
def _validated_create_connection(address, timeout, source_address, *, allowed_private_hosts):
    host, port = address
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if host.lower() not in allowed_private_hosts:
        for *_, sockaddr in infos:
            candidate = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
            if not candidate.is_global:
                raise ValueError(f"non-public address is not allowed: {candidate}")
    last_error: Exception | None = None
    for *_, sockaddr in infos:
        try:
            return socket.create_connection(sockaddr[:2], timeout, source_address)
        except OSError as exc:
            last_error = exc
    raise last_error or OSError("no addresses to connect to")
```

検証項目2は「hostnameは接続時に再解決されない(検証と接続が同一のaddrinfo呼び出し結果を使う)」だった。

**(後)**

```python
def _validated_create_connection(address, timeout, source_address, *, allowed_private_hosts):
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if host.lower() not in allowed_private_hosts:
        for *_, sockaddr in infos:
            candidate = ipaddress.ip_address(sockaddr[0].split("%", 1)[0])
            if not candidate.is_global:
                raise ValueError(f"non-public address is not allowed: {candidate}")
    last_error: Exception | None = None
    for family, socktype, proto, _, sockaddr in infos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            if sock is not None:
                sock.close()
    raise last_error or OSError("getaddrinfo returned an empty list")
```

検証項目2を「`_validated_create_connection`内の`socket.getaddrinfo`は1回だけ呼ばれ、取得した`family`/`socktype`/`proto`/`sockaddr`を直接`socket.socket(...).connect(sockaddr)`へ使用する。`socket.create_connection`は呼ばない」に改める。

**理由。** 改訂前のコードは検証済みaddrinfoから`sockaddr[:2]`(host, port)だけを取り出し、`socket.create_connection(sockaddr[:2], ...)`へ渡していた。ところが`socket.create_connection`自体が内部で`host, port = address`のあと`getaddrinfo(host, port, 0, SOCK_STREAM)`を再実行する。渡している`host`は既に数値IPであるためネットワーク越しのDNS問い合わせには発展せずSSRF上の実害はないが、A節が明記した「接続は検証済みaddrinfo集合の中だけで試み、hostnameを再解決しない」という設計原則と実装が食い違っていた。加えて`sockaddr[:2]`への切り詰めはIPv6の`(address, port, flowinfo, scope_id)`4要素タプルから`flowinfo`/`scope_id`を失わせる、独立した正確性の問題だった。改訂後は、最初の1回の`getaddrinfo`が返した`family`/`socktype`/`proto`/`sockaddr`をそのまま使って`socket.socket(...)`を生成し`connect(sockaddr)`する、CPython自身の`socket.create_connection`と同じ構造に置き換え、`getaddrinfo`の再実行と`sockaddr`の切り詰めを両方なくした。

**評価の経緯。** レビュー6(ChatGPT)がA節のコード例と検証項目2の不一致を指摘し、標準ライブラリの`socket.create_connection`のソースを実際に確認した(`python -c "import socket, inspect; print(inspect.getsource(socket.create_connection))"`)ところ、`for res in getaddrinfo(host, port, 0, SOCK_STREAM):`という再実行が実在した。レビュー7(Claude Code)で採用と判断し、本改訂で最終案へ反映した。

### 改訂2 — Claude Code (2026-08-20)

**該当箇所:** B節の「判定方式」段落。

**(前)**

> **判定方式。** `allow_private_urls: bool`という全面許可フラグは残さない。検証関数は`allowed_private_hosts: frozenset[str]`を受け取り、**解決前のhostname文字列(正規化済み)がこの集合に含まれる場合のみ**、privateアドレスチェックをその1ホストに限りスキップする。1ホストを許可しても他の全private IPを許可しないよう、集合要素ごとの一致で判定する。

**(後)**

> **判定方式。** `VaultConfig`(`sync.py`・RSS fetch)の経路では`allow_private_urls: bool`という全面許可フラグは残さない。検証関数は`allowed_private_hosts: frozenset[str]`を受け取り、**解決前のhostname文字列(正規化済み)がこの集合に含まれる場合のみ**、privateアドレスチェックをその1ホストに限りスキップする。1ホストを許可しても他の全private IPを許可しないよう、集合要素ごとの一致で判定する。
>
> **legacy `Config`(`feedian/config.py`)経路は対象外とし、全面フラグを維持する。** `feedian/__main__.py`はCOMMANDSに載らない別のCLIモード(`--source hatena`等)を持ち、`feedian/config.py`の`Config`(`VaultConfig`とは別のdataclass)を使って`fetch_page_text`を4箇所(`__main__.py:639,843,900,1340`)から直接呼んでいる。`Config.allow_private_urls: bool`はhost単位ではない全面許可フラグであり、`NetworkPolicy`の設計とは相容れない。この経路専用に、`Config`から`FetchPolicy`を組み立てる小さなアダプタ(`allow_private_urls=True`なら全アドレスを許可する`NetworkPolicy`相当の扱いとする)を設け、既存のCLI挙動を変えない。新しい`VaultConfig.fetch["allow_private_hosts"]`の設計をこのアダプタへ拡張することはしない。

**理由。** 実装着手前に`fetch_page_text`の全呼び出し元を`grep`で洗い出したところ、B節が想定していなかった第4の経路が見つかった。`feedian/cli.py`の`COMMANDS`(`init`/`sync`/`restore`等)とは別に、`feedian/__main__.py`は`feedian/config.py`の`Config`という別系統のdataclassを使う旧来のCLIモード(引数`--source hatena`等、`is_modern_command`が偽と判定する経路)を持ち続けており、そこから`fetch_page_text`を直接呼んでいた。`Config.allow_private_urls: bool`は`VaultConfig.fetch["allow_private_hosts"]`と異なり、private/localアドレスへの接続を丸ごと許可するかどうかの単純な真偽値であり、host単位の許可リストという概念を持たない。B節はこの経路の存在を見落としたまま「全面許可フラグは残さない」と無条件に書いていたが、`fetch_page_text`のシグネチャを`policy: FetchPolicy`のみを受け取る形に変えると、この4箇所は確実に壊れる。

要件所有者の判断(2026-08-20)により、legacy経路に限り全面フラグを温存し、新規のVaultConfig系統には拡張しないことにした。理由は、この経路がローカルでの単発処理・移行前の後方互換用であり、影響範囲を広げてまで統一する必要がないためである。

**評価の経緯。** implementerへ委譲する前に、`grep -rn "fetch_page_text(" feedian/*.py`で全呼び出し元を確認し、`__main__.py:639,843,900,1340`が`feedian/config.py`の`Config`経由であることを特定した。ユーザーへ選択肢を提示し、「legacy経路に限り全面フラグを温存する」という回答を得た。

## 草案

### 背景

ChatGPTによる外部レビューで5点の実装上の問題が指摘された。いずれもgraftとソースコードで裏付けを確認済みである(該当箇所は各節に記す)。設計のやり直しを要するものはなく、既存の境界(SSRF防御・設定配線・restore検証・設定パース・RSS解析)にある穴を塞ぐ修正である。

### 目的と非目的

**目的**

- SSRF防御のTOCTOU(DNS rebinding)を閉じる。
- `fetch`設定(`html_max_bytes`/`document_max_bytes`/`allow_private_hosts`)を実際の取得経路へ配線する。
- snapshot/restoreの検証を、archive自身に同梱されたmanifestではなくGit tagに固定された値を信頼点にする。
- 設定パースの`bool`変換をFeedian全体で「fail-fast」の方針に揃える。
- `xml.etree.ElementTree`の要素truth-testingによるDeprecationWarningを解消する。

**非目的**

- Playwright(Chromium)側のDNS解決そのものをIPレベルで完全に固定することは行わない。ChromiumはPlaywrightのAPI越しに任意の宛先IPを強制する手段を持たず、それを実現するには専用プロキシのようなインフラが要る。「最後の数%を閉じる機構を作らない」というAGENTS.mdの指針に従い、恒久キャッシュの除去とrequestごとの再検証までを範囲とする。
- fetch設定のスキーマ自体の見直しは行わない。既存キーを実配線するだけであり、キーの追加・削除・意味変更は伴わない。
- CI上でのPlaywright実ブラウザ統合テストの追加は別仕様とする。本仕様はコード側の修正のみを扱う。

### 設計案

#### A. SSRF防御のDNS rebinding対策

**問題。** `validate_fetch_url`(`feedian/extract.py:916-932`)は`socket.getaddrinfo`で解決したIPを検証するだけで、実際の接続は`urllib`のopenerが別途行うDNS解決に依存する(`feedian/extract.py:279`付近)。検証と接続の間にDNSが変化すれば、検証をすり抜けてprivateアドレスへ接続し得る。

**direct fetch経路の対策。** 検証で得たIPをそのまま接続に使う。`http.client.HTTPSConnection`を継承し、`connect()`を検証済みIPへの`socket.create_connection`に差し替え、TLSのSNI/Hostヘッダは元のhostnameのまま送る。

```python
class _PinnedHTTPSConnection(HTTPSConnection):
    def __init__(self, host, *, resolved_ip, context, **kwargs):
        super().__init__(host, context=context, **kwargs)
        self._resolved_ip = resolved_ip

    def connect(self):
        sock = socket.create_connection((self._resolved_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
```

`validate_fetch_url`を、検証に使ったIPを呼び出し元へ返す形に変える(現状は成功時に何も返さない)。`fetch_page_text`は返ったIPを`_PinnedHTTPSConnection`へ渡す。HTTPスキームの場合はTLSがないため`socket.create_connection`のみで足りる。

**リダイレクト経路も同様に扱う。** `SafeRedirectHandler.redirect_request`(`feedian/extract.py:115-122`)は現在検証のみ行い、実接続は次のopener呼び出しに委ねている。ホップごとに新しい`_PinnedHTTPSConnection`を生成し直す必要があり、`build_opener`のHandlerチェーンでコネクションクラスを差し替える具体的な実現方法は詳細設計時に詰める。

**Browser fallback経路の対策。** `_validated_browser_hosts`による恒久キャッシュ(`feedian/extract.py:874`付近)を廃止し、`page.route()`のたびに`validate_fetch_url`を必ず呼ぶ。ChromiumのDNS解決自体をFeedian側から強制することはできないため、これは「同一hostへの2回目以降の検証を省略する」バグの除去であり、Chromiumの実接続DNSとの間のTOCTOUを完全には閉じない。この残存リスクは受け入れる不正確さとして明記する。

#### B. FetchPolicyの導入とconfig配線

**問題。** `MAX_HTML_BYTES`/`MAX_DOCUMENT_BYTES`(`feedian/extract.py:25-26`)はモジュール定数として固定されており、`config.fetch["html_max_bytes"]`等は一切読まれない(grepで使用箇所ゼロを確認済み)。`allow_private_hosts`(`feedian/vault.py:72`)もデフォルト値の定義以外どこからも参照されない。`sync.py:390`と`sync.py:696`は`allow_private_urls=False`を直書きしている。

**設計。**

```python
@dataclass(frozen=True)
class FetchPolicy:
    html_max_bytes: int
    document_max_bytes: int
    allow_private_hosts: frozenset[str]
    timeout_seconds: int
    browser_timeout_seconds: int
```

`feedian/vault.py`に`fetch_policy(config: VaultConfig) -> FetchPolicy`を追加し、既存の`fetch_retry_settings`と同じ場所で検証する。`html_max_bytes`/`document_max_bytes`は`positive_int_setting`で検証する(bool拒否・1以上)。`timeout_seconds`/`browser_timeout_seconds`は既存の`fetch_retry_settings`が持つ値をそのまま流用するか、`FetchPolicy`へ統合するかは詳細設計時に決める。

`fetch_page_text`・`render_html_with_browser`・`fetch_page_text_with_browser`のシグネチャを、個別引数の羅列ではなく`policy: FetchPolicy`を受け取る形に変える。`sync.py`の呼び出し箇所はconfigから`fetch_policy(config)`を1回構築し、全呼び出しへ同じインスタンスを渡す。

**`allow_private_hosts`の判定方式。** `allow_private_urls: bool`という全面許可フラグは残さない。`validate_fetch_url`は`allowed_private_hosts: frozenset[str]`を受け取り、**解決前のhostname文字列(小文字化)がこの集合に含まれる場合のみ**、privateアドレスチェックをその1ホストに限りスキップする。1ホストを許可しても他の全private IPを許可しないよう、集合要素ごとの一致で判定し、`bool(allow_private_hosts)`のような真偽値化は行わない。

#### C. restore検証をGit tagの値へ固定する

**問題。** `restore_database`(`feedian/restore.py:13-47`)はarchive内の`manifest.json`が持つ`database.sha256`とのみ照合する。この値はarchive自身に同梱されており、改ざんしたDBと辻褄を合わせたmanifestは自己整合するため検出できない。一方`create_snapshot`(`feedian/snapshots.py:38-127`)はarchive自体のsha256を`.feedian/snapshot.json`へ記録し、Gitコミット・タグとして固定している(`feedian/snapshots.py`の`archive_sha256`書き込み箇所)。

**設計。** restoreはarchiveのsha256を、archive自身からではなく`git show <tag>:.feedian/snapshot.json`で読んだ値と照合してから展開する。

```
1. restoreはarchiveパスに加えてtag名を必須引数として受け取る
2. git show <tag>:.feedian/snapshot.json を読み、archive.sha256を取り出す
3. 指定されたarchiveファイルのsha256を計算し、2の値と一致することを確認する
4. 一致しなければ即座に失敗し、展開しない
5. (従来通り) 7z test → 展開 → manifest.json内のdatabase.sha256と照合 → PRAGMA integrity_check
```

`restore_database`のシグネチャに`tag: str`を追加する破壊的変更になる。Git管理下のVaultでない、またはタグが存在しない場合は復元できない(fail-fast)。既存の「archiveファイルだけ渡せば復元できる」経路は廃止する。

#### D. 設定パースの`bool`厳密化

**問題。** `ProviderSettings.enabled`(`feedian/vault.py:411`)と`RssFeedSettings.enabled`(`feedian/vault.py:474`)は`bool(value.get("enabled", True))`で変換しており、`"enabled": "false"`(文字列)を渡しても`bool("false") == True`となり無効化のつもりが有効になる。`workers`等で使われている`positive_int_setting`のような厳密チェックと不統一である。

**設計。** `feedian/vault.py`に`strict_bool_setting(name: str, value: object) -> bool`を追加し、`positive_int_setting`と同じ形で`bool`以外を`ValueError`にする。

```python
def strict_bool_setting(name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false, got {value!r}")
    return value
```

`_parse_providers`と`_parse_rss_feed`の`enabled`変換をこれに置き換える。デフォルト値(`True`)は`value.get("enabled", True)`のまま維持し、キーが実際に存在する場合のみ型を検証する。

#### E. ElementTree truth-testing警告の解消

**問題。** `feedian/rss.py:113`の`channel = root if atom else (_first_descendant(root, "channel") or root)`は`xml.etree.ElementTree.Element`を`or`で真偽判定しており、標準ライブラリのDeprecationWarningが出る。`feedian/rss.py:5`が示す通り`lxml`ではなく`xml.etree.ElementTree`由来である。

**設計。** 明示的な`is None`判定に置き換える。

```python
channel = root if atom else _first_descendant(root, "channel")
if channel is None:
    channel = root
```

### 検証

```
python -m pytest -q
```

**A. SSRF/DNS rebinding**

1. `validate_fetch_url`で検証したIPが実際の接続先として使われる(モックした`socket.create_connection`の呼び出し引数を確認)。
2. Host/SNIは検証済みIPではなく元のhostnameのまま送られる。
3. リダイレクト先ごとに再度IPをピン留めして接続する。
4. Browser fallbackで同一hostへの2回目のrequestでも`validate_fetch_url`が毎回呼ばれる(恒久キャッシュが存在しないことを確認)。

**B. FetchPolicy**

5. `config.fetch["html_max_bytes"]`を変更すると`fetch_page_text`が実際にその上限で打ち切る。
6. `config.fetch["document_max_bytes"]`も同様。
7. `allow_private_hosts`に1ホストだけ指定すると、そのホストのみprivateアドレスチェックをスキップし、他のprivateホストは従来通り拒否される。
8. `sync.py`の呼び出し箇所に`allow_private_urls=False`のハードコードが残っていない。

**C. restore検証**

9. archiveのsha256がGit tag側の`.feedian/snapshot.json`と一致しない場合、展開前に失敗する。
10. 一致する場合は従来通りDB sha256照合とintegrity_checkまで進む。
11. tagが存在しない、またはGitリポジトリでない場合は復元できない。

**D. bool厳密化**

12. `"enabled": "false"`(文字列)や`"enabled": 1`(整数)を渡すと`ValueError`になる。
13. `"enabled": true`/`"enabled": false`(JSON真偽値)は従来通り動作する。

**E. warning解消**

14. RSS関連のテストで`xml.etree.ElementTree`由来のDeprecationWarningが出なくなる。

### 未決事項

- A節のリダイレクト先ピン留めの具体的な実現方法(`build_opener`のHandlerチェーンでの差し替え方)は詳細設計時に確定する。
- B節で`timeout_seconds`/`browser_timeout_seconds`を`FetchPolicy`へ統合するか、既存の`fetch_retry_settings`に残すかは詳細設計時に決める。
- C節でtag未指定時の後方互換性(既存の「archiveのみで復元」を許すか)は、要件所有者の判断を要する。

## レビュー

### レビュー1 — Claude Code (2026-08-20)

結論は**要修正**である。C節はそのまま採用できる。B節に見落としが1点ある。A節はTOCTOU解消という方向性は正しいが、実現方法を変える必要がある。引用された`file:line`はすべて実コードで再現を確認した。事実誤認はない。

| 草案の提案 | 採否 | 理由 |
|---|---|---|
| A: `validate_fetch_url`が検証済みIPを返し、それを`_PinnedHTTPSConnection`へ渡す | 不採用 | リダイレクトの各ホップをどう扱うか(草案の未決事項)が、この設計だと本質的に解決しない。指摘1で置き換える |
| A: `_validated_browser_hosts`の恒久キャッシュを廃止する | 採用 | |
| A: Chromium側のDNS固定を対象外とする判断 | 修正して採用 | 判断自体は妥当だが、根拠として挙げた「Playwrightに制御手段がない」が事実と異なる。指摘2で修正する |
| B: `FetchPolicy`の導入とconfig配線 | 採用 | |
| B: `allow_private_hosts`をホスト単位の集合一致で判定する | 採用 | |
| C: `tag`を必須にしてGit tag経由でarchive sha256を照合する | 採用 | |
| C: tag未指定時のフォールバックを設けない(未決事項への回答) | 採用 | |
| D: `strict_bool_setting`の導入 | 採用 | |
| E: `is None`判定への置き換え | 採用 | |

#### 指摘1: IPをthreadingするのではなく、接続クラスの`connect()`内で解決する — 重大度: 高

草案は`validate_fetch_url`が検証済みIPを返し、それを呼び出し元が`_PinnedHTTPSConnection`へ渡す設計を提案する(草案`:31-39`)。この設計はリダイレクトで破綻する。`SafeRedirectHandler.redirect_request`(`feedian/extract.py:115-122`)は同じ`HTTPSHandler`チェーンへ再入するため、ホップ2の接続を作る時点で「どの検証済みIPを使うか」を外から渡す経路がなく、草案自身が未決事項として残していた(草案`:33`)。

解決は、IPの受け渡しをやめ、**`http.client.HTTPSConnection`を継承したクラスの`connect()`内で毎回`getaddrinfo`→検証→接続を完結させる**ことである。

```python
class _ValidatingHTTPSConnection(HTTPSConnection):
    def __init__(self, host, *, allowed_private_hosts, **kwargs):
        super().__init__(host, **kwargs)
        self._allowed_private_hosts = allowed_private_hosts

    def connect(self):
        address = _resolve_and_validate(self.host, self.port, self._allowed_private_hosts)
        sock = socket.create_connection((address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
```

`HTTPSHandler`を継承し、`https_open`で`self.do_open(_ValidatingHTTPSConnection, req, context=self._context)`を呼ぶ形にする(`urllib.request.AbstractHTTPHandler.do_open`は接続クラスをコールバックとして受け取る標準の仕組みであり、追加の依存ライブラリは要らない)。

この形にすると、リダイレクトは自然に解決する。各ホップは新しい`Request`から新しい接続インスタンスを作るため、ホップごとに独立して解決・検証・接続が行われる。`validate_fetch_url`の戻り値を変える必要はなく、`SafeRedirectHandler`側の変更も不要になる。

**採否: 不採用(設計を置き換える)。** DNS rebindingを閉じるという目的は変わらないが、実現方法を「検証結果を上位層へ運ぶ」から「接続そのものが検証を内包する」へ変える。

#### 指摘2: Chromium固定を諦める理由が事実と異なる — 重大度: 低

草案は「ChromiumはPlaywrightのAPI越しに任意の宛先IPを強制する手段を持たず、それを実現するには専用プロキシのようなインフラが要る」と述べる(草案`:41`)。実際にはChromiumは`--host-resolver-rules=MAP host ip`という起動引数でホスト名解決を上書きできる。ただしこれは`launch()`時に固定されるプロセス全体設定であり、`feedian/extract.py:852-868`の`_browser`はモジュールレベルの単一インスタンスとして一度だけ`launch(headless=True)`されるため、**request単位で異なるルールを適用することはできない**。

対象外とする判断自体は妥当である。ただし記録する理由は「手段が存在しない」ではなく「手段はあるがプロセス起動時に1回しか設定できず、`_browser`を単一インスタンスとして使い回す現行構造とは両立しない」とすべきである。理由を正確に記録しておかないと、後日「専用プロキシを作れば解決する」という誤った代替案の再検討を招く。

**採否: 修正して採用。** 対象外とする結論は変えないが、根拠の記述を訂正する。

#### 指摘3: `BrowserCandidate.allow_private_urls`がスレッド境界を越える — 重大度: 中

`BrowserCandidate`(`feedian/extract.py:74-83`)は`allow_private_urls: bool`フィールドを持ち、ワーカースレッドからmainスレッドへ処理を引き継ぐ際に運ばれる(`complete_browser_fallback`、`feedian/extract.py:511-545`)。B節は`allow_private_urls: bool`を`allowed_private_hosts: frozenset[str]`へ置き換える設計だが、このフィールドへの言及が草案の設計・検証項目のいずれにもない。ここを直さないと、ワーカースレッド経由の処理だけ旧来の全面許可フラグのまま残る。

**採否: 採用(見落としの追加)。** `BrowserCandidate.allow_private_urls`の型も`frozenset[str]`へ変更し、検証項目に追加する。

#### 指摘4: 平文HTTPとproxy経由がpinningから漏れる — 重大度: 中

`fetch_page_text`の`build_opener(HTTPSHandler(context=context), SafeRedirectHandler(allow_private_urls))`(`feedian/extract.py:282`)は`HTTPHandler`を明示していない。`urllib.request.build_opener`は明示されなかった既定クラス(`HTTPHandler`・`ProxyHandler`等)を自動で追加するため、指摘1の`_ValidatingHTTPSConnection`をHTTPSにだけ適用すると、**平文`http://`のリクエストは無検証の既定`HTTPHandler`を素通りする**。またproxy環境では`ProxyHandler`が`HTTPS_PROXY`等の環境変数を読み、実際の接続先はproxyになるため、`req.host`(=対象URLのホスト)をpinningする設計はproxy経由では意味を持たない。

**採否: 採用(範囲の明確化)。** `_ValidatingHTTPSConnection`と対になる`_ValidatingHTTPConnection`をHTTP用にも用意し、両方を明示的に`build_opener`へ渡す。proxy経由の扱い(禁止するか、pinningを諦めて素通りさせるか)は詳細設計時に明記する。

#### 設計への影響

指摘1により、A節の設計案は次のように置き換わる。

- `validate_fetch_url`の戻り値変更は不要。既存のシグネチャのまま維持する。
- `_PinnedHTTPSConnection`という「IPを外から受け取る接続クラス」は採らない。代わりに`_ValidatingHTTPSConnection`/`_ValidatingHTTPConnection`という「`connect()`内で自己完結して解決・検証する接続クラス」を導入する。
- A節の未決事項(リダイレクト先ピン留めの実現方法)は解消する。ホップごとに独立した接続インスタンスが自然に処理するため、`SafeRedirectHandler`への追加変更は不要。
- `HTTPHandler`相当のクラスをHTTP用にも用意し、`build_opener`へ両方明示する。proxy経由の扱いは詳細設計時の判断とする。

#### 検証項目への影響

- 草案の検証項目1〜3を、「`_ValidatingHTTPSConnection.connect()`が解決・検証・接続を1回のホスト単位操作内で完結する」ことを検証する形へ差し替える。
- 平文`http://`のリクエストも検証済み接続を経由することを検証項目に追加する(指摘4)。
- `BrowserCandidate.allow_private_urls`が`frozenset[str]`として渡ることを検証項目に追加する(指摘3)。

### レビュー2 — ChatGPT (2026-08-20)

結論は **要修正** です。Claude Code のレビューでかなり良くなっていますが、**そのレビューにも重要な見落としがあります**。このまま「最終案」に上げるより、もう1回草案を改訂してから確定するのがよいです。
特にA節は、現在の目的である「SSRF防御のTOCTOUを閉じる」を満たしていません。
#### 追加指摘1：RSS取得経路が丸ごと漏れている — 重大度: 高
これが一番大きいです。
草案もClaudeレビューも `fetch_page_text()` を中心に見ていますが、`feedian/rss.py` の `fetch_rss_items()` にも同じ構造があります。
現在RSS取得は、
```python
validate_fetch_url(feed_url, allow_private_urls=allow_private_urls)
opener = build_opener(
    HTTPSHandler(context=ssl.create_default_context()),
    SafeRedirectHandler(allow_private_urls)
)
with opener.open(request, ...):
    ...
```
です。つまり、
```text
DNS解決して検証
↓
urllibが再度DNS解決
↓
接続
```
という、まさにA節で直そうとしているTOCTOUがRSSにもあります。
しかも `sync.py` のRSS経路は `fetch_rss_items()` をそのまま呼んでおり、FetchPolicyもprivate-host設定も渡していません。
したがってA/Bは、
```text
fetch_page_text
だけ修正
```
ではなく、
```text
共通の安全なHTTP opener / connection
        ↓
  ┌─────┴─────┐
page fetch    RSS fetch
```
にすべきです。
例えば責務として、
```python
build_fetch_opener(policy)
```
あるいは、
```python
ValidatingHTTPHandler
ValidatingHTTPSHandler
```
を `extract.py` か独立モジュールに置き、**ページ取得とRSS取得が同じ安全なtransportを使う**設計にするのがよいです。
これならSSRF修正を2か所に複製する必要もありません。
---
#### 追加指摘2：proxyの扱いは「詳細設計で決める」では遅い — 重大度: 高
Claudeレビューの指摘4は正しいのですが、
> proxy経由の扱い（禁止するか、pinningを諦めて素通りさせるか）は詳細設計時に明記する
では不十分です。
**これは実装詳細ではなく、SSRF防御のセキュリティモデルそのものです。**
現在の `build_opener()` は独自の `ProxyHandler` を渡していないため、urllibの標準`ProxyHandler`が環境変数のproxyを自動利用できます。現行コードもその状態です。
proxy経由では概念的に、
```text
Feedian
  ↓ TCP
Proxy
  ↓ DNS解決
Target
```
となるため、FeedianがtargetのIPを検証・pinしても、**proxyがhostnameを再解決する構成ならorigin側のDNS rebindingをFeedianから保証できません**。
よって最終仕様ではどちらかを決める必要があります。
私なら今回の仕様では、
```python
ProxyHandler({})
```
を明示して、**Feedianのコンテンツ取得は環境proxyを使用しない**とします。
これが最も単純で、SSRFについて
> direct fetchでは「検証したIPに実際に接続する」
という保証を本当に言えるようになります。
proxy対応が必要になったら、別仕様で「明示的に設定されたtrusted proxy」を導入した方が設計がきれいです。
もし現在のproxy対応を維持するなら、目的を
> DNS rebindingを閉じる
とは書けず、
> proxyを使用しないdirect connectionについて閉じる
と限定する必要があります。
---
#### 追加指摘3：Claude案の`connect()`上書きは少し危ない — 重大度: 中〜高
Claudeレビューでは、
```python
class _ValidatingHTTPSConnection(HTTPSConnection):
    def connect(self):
        address = _resolve_and_validate(...)
        sock = socket.create_connection(...)
        self.sock = self._context.wrap_socket(...)
```
という方向を提案しています。
IPを上位層からthreadingするよりは良いのですが、**`connect()`そのものを再実装するのは避けた方がいい**です。
標準の `HTTPConnection.connect()` / `HTTPSConnection.connect()` には、
* proxy tunnel
* `TCP_NODELAY`
* `source_address`
* TLS wrapping
* tunnel時のSNI切替
などの処理があります。
特にHTTPS proxyでは `_tunnel_host` を考慮するので、自前の `connect()` を書くと標準挙動を取りこぼしやすいです。
より安全なのは、標準`connect()`を残して、**実際にsocketを作る部分だけ差し替える**ことです。
`HTTPConnection` は内部で接続関数を `self._create_connection` として持っているので、概念的には、
```python
class _ValidatingHTTPConnection(HTTPConnection):
    def __init__(...):
        super().__init__(...)
        self._create_connection = self._validated_create_connection
```
の方がよいです。
こうすれば、
```text
標準 HTTPConnection.connect()
    ↓
検証済み接続関数
    ↓
標準の残りの処理
```
となります。
HTTPSも同じ構造を利用できるので、標準ライブラリの接続ロジックを極力壊しません。
ただしproxyを今回無効化するなら、この問題もかなり単純になります。
---
#### 追加指摘4：複数IPをどう扱うかが未定義 — 重大度: 中
現在の `validate_fetch_url()` は `getaddrinfo()` が返した**すべてのIP**を検査し、
```python
for ... sockaddr in addresses:
    if not ipaddress.ip_address(...).is_global:
        raise ValueError(...)
```
としています。
これは重要です。
例えば、
```text
example.com
 ├ 203.0.113.x
 └ 127.0.0.1
```
のようなmixed responseなら拒否します。
Claude案の、
```python
address = _resolve_and_validate(...)
socket.create_connection((address, port))
```
だと、`_resolve_and_validate()`が**単一IPを返す**ようにも読めます。
ここは仕様に、
> 1回の`getaddrinfo()`で得た全候補を検査し、1つでも非許可private/non-global addressを含む場合は全体を拒否する。許可されたaddrinfo集合の中だけで接続候補を試す。接続のためにhostnameを再解決しない。
と明記した方がいいです。
IPv6→IPv4 fallbackなどもあるため、「先頭IPだけ使う」設計にはしない方がよいです。
---
#### 追加指摘5：目的Aの書き方が現状と矛盾している — 重大度: 中
草案の目的は、
> SSRF防御のTOCTOU(DNS rebinding)を閉じる。
ですが、非目的ではBrowserについて、
> Chromiumの実接続DNSとの間のTOCTOUを完全には閉じない
としています。
これは文章上、両立しません。
目的を例えば、
> urllibを用いるdirect fetch経路ではDNS検証と実接続を一致させ、DNS rebindingによるTOCTOUを閉じる。Browser fallbackでは恒久的な検証キャッシュを廃止し、各requestを再検証する。ただしChromium内部の名前解決とのTOCTOUは残存リスクとして受容する。
くらいまで限定するのが正確です。
さらに前述のRSS経路も含めるべきです。
---
#### B節：FetchPolicyにはもう1段修正が必要
`FetchPolicy`の導入自体は賛成です。
ただし現在の草案には、
```python
allow_private_hosts: frozenset[str]
```
をどう作るかの入力検証がありません。
`fetch`は自由形式のdictなので、例えばユーザーが、
```json
"allow_private_hosts": "localhost"
```
と書いたとき、単純に
```python
frozenset(value)
```
すると、
```python
{'l', 'o', 'c', 'a', ...}
```
になります。
したがって仕様に明記すべきなのは、
```text
allow_private_hostsはJSON array of stringのみ許可
各要素はtrim
小文字化
空文字拒否
重複除去
```
です。
可能ならhostname正規化関数を一つ作り、
```python
normalize_hostname()
```
を、
```text
config側
URL側
```
双方で使った方がよいです。
少なくとも、
```text
Example.COM
example.com
example.com.
```
の扱いを一貫させておくべきです。
---
#### `BrowserCandidate`の指摘はClaudeレビュー通り
これは採用でよいです。
現在実装上、
```python
class BrowserCandidate:
    ...
    allow_private_urls: bool
```
としてworker→main thread間を渡っています。
したがって単に関数引数を変更するだけでは足りません。
ただ、`FetchPolicy`がimmutableなら、
```python
class BrowserCandidate:
    ...
    policy: FetchPolicy
```
と**policyごと持たせる**方が自然です。
`allowed_private_hosts`だけ別途渡すより、後から設定が増えたときの配線漏れも減ります。
---
#### C節：方向性は良いが「Git tagが信頼点」の意味を明記した方がいい
Cの基本案には賛成です。
現在のrestoreは実際に、
```text
archive内manifest
      ↓
archive内DB
```
だけで自己完結して検証しています。
そこを、
```text
Git tagのcommit
     ↓
.feedian/snapshot.json
     ↓
archive SHA-256
     ↓
archive
     ↓
manifest DB SHA
     ↓
SQLite integrity_check
```
とするのは強化になります。
ただし、「Git tagに固定」という表現は少し強すぎます。
tag名そのものは参照なので、理論上は付け替えられます。署名付きtagでもありません。
したがってこれは、
> **trusted Git repositoryをarchive外の信頼点とする**
設計です。
脅威モデルを一文追加して、
> 本検証は、Git repositoryのcommit/tag情報は信頼でき、Release archiveのみが破損・改ざんし得るという前提に立つ。Git repository自体が攻撃者に書き換えられた場合の真正性は保証しない。
とするのが正確です。
---
#### fresh cloneからの復元も考えておきたい
現在、
```python
download_and_restore(vault_root, tag)
```
はGitHub Releaseからarchiveを取得できますが、その後 `restore_database()` を呼んでいます。
C案ではローカルにtagが存在しないと復元不可になります。
災害復旧で、
```text
PC死亡
↓
private repoをfresh clone
↓
ReleaseからDBをrestore
```
をやるなら、tagがローカルにまだ無いケースは普通にあります。
なので、
```text
download_and_restore()
    ↓
必要なら origin から指定tagを取得
    ↓
git show <tag>:.feedian/snapshot.json
```
まで仕様に入れておくのがおすすめです。
一方、
```text
restore_database(local_archive, tag)
```
はoffline用途として「ローカルtag必須」でよいでしょう。
つまり、
```text
restore_database
    tagは必須、ローカルGitを信頼
download_and_restore
    remoteからtagを確保してからrestore_database
```
という責務分担です。
---
#### D節：「Feedian全体」という表現だけ直したい
実装案自体は妥当です。
ただし目的に、
> 設定パースのbool変換をFeedian全体でfail-fast
とありますが、設計で直しているのは、
```text
ProviderSettings.enabled
RssFeedSettings.enabled
```
です。
Feedianにはlegacy configも残っていますし、文字通り「全体」ではありません。
なので、
> 現行Vault configにおけるboolean設定の型検証をfail-fast方針に揃える
くらいが正確です。
---
#### E節はそのまま採用でよい
ここは問題ありません。
実際に `rss.py` は、
```python
import xml.etree.ElementTree as ET
```
で、
```python
channel = root if atom else (_first_descendant(root, "channel") or root)
```
となっています。
明示的な`is None`へ変更する案で十分です。
---
#### 私なら未決事項をこう整理する
今の3つの未決事項のうち、**2つは確定前に解消すべき**です。
* **proxyの扱い** → 今決める。推奨はdirect fetchで自動proxyを無効化。
* **timeoutをFetchPolicyへ統合するか** → 今決める。これはAPI設計なので仕様事項。
* **archive-only restoreを残すか** → Claude案通り「残さない」で確定してよい。
timeoutについては、私なら `FetchPolicy` に入れます。
```python
@dataclass(frozen=True)
class FetchPolicy:
    html_max_bytes: int
    document_max_bytes: int
    allowed_private_hosts: frozenset[str]
    timeout_seconds: int
    browser_timeout_seconds: int
```
一方、
```text
retry_base_minutes
retry_max_days
terminal_http_statuses
terminal_failure_kinds
```
は「取得方法」ではなく「再試行判断」なので `FetchRetrySettings` に残します。
つまり、
```text
FetchPolicy
  = 1回のfetchをどう安全に行うか
FetchRetrySettings
  = 失敗したfetchをいつ再実行するか
```
と責務を分けるときれいです。
---
#### 最終的な評価
Claudeレビューの4指摘は基本的に採用してよいです。ただし、**Claudeの指摘1の`connect()`再実装はそのままコード化しない方がよく、connectionの低レベル接続関数だけを差し替える設計を検討すべき**です。
そして追加で最低限、草案へ以下を反映してから確定するのを勧めます。
1. **RSS取得もDNS rebinding対策の対象に追加**
2. **HTTP/HTTPSの安全なtransportを共通化**
3. **proxyを今回どう扱うか確定**
4. **DNSの全addrinfoを検証し、その結果だけに接続すると明記**
5. **`allow_private_hosts`自体の型・hostname正規化を追加**
6. **BrowserCandidateはFetchPolicyを保持**
7. **C節にGit repositoryを信頼する脅威モデルを明記**
8. **fresh clone時のtag取得方法を明記**
9. 目的の「TOCTOUを閉じる」をdirect fetchに限定して正確化
特に **1のRSS漏れは、今回このレビューで新たに見つかった実質的なバグです**。ここを追加すれば、仕様全体の完成度はかなり上がります。

### レビュー3 — Claude Code (2026-08-20)

結論は**すべて採用**である。引用された内容はすべて実コードで再現を確認した。事実誤認はない。

| ChatGPTの指摘 | 採否 | 理由 |
|---|---|---|
| 追加指摘1: `fetch_rss_items`がA/B節の対象から漏れている | 採用 | `feedian/rss.py:31-53`に独自の`build_opener(HTTPSHandler(...), SafeRedirectHandler(...))`があり、`extract.py`側の修正が及ばないことを確認した。設計対象の欠落であり、レビュー1・草案いずれも見落としていた |
| 追加指摘2: proxyの扱いを詳細設計へ先送りしない | 採用 | Feedianはproxy対応をREADME等どこにも文書化しておらず、現在の挙動は`build_opener`が既定`ProxyHandler`を暗黙に追加する副作用に過ぎない。意図された機能を壊すわけではないため、`ProxyHandler({})`を明示してcontent取得を直接接続に限定する判断は低リスクで採用できる |
| 追加指摘3: `connect()`を丸ごと上書きせず`_create_connection`を差し替える | 採用 | Python標準ライブラリの`http.client.HTTPConnection.connect()`は`self._create_connection(...)`という差し替え可能なインスタンス属性を介して接続しており、`HTTPSConnection.connect()`も`super().connect()`経由でこれを使う。差し替えるだけでTCP_NODELAYやtunnel処理(`_tunnel_host`)を失わずに済む。レビュー1指摘1の`_ValidatingHTTPSConnection`のスケッチはこの方式へ置き換える |
| 追加指摘4: 全addrinfoを検証し、その集合の中だけで接続する | 採用 | 現行`validate_fetch_url`(`feedian/extract.py:924-932`)は`getaddrinfo`が返す全アドレスを検査し、1つでも非globalなら全体を拒否している。レビュー1指摘1の`_resolve_and_validate`が単一IPを返す書き方だったのは不正確であり、「検証済みaddrinfo集合の中だけで接続を試みる」設計に直す |
| 追加指摘5: 目的の文言をdirect fetchとRSSに限定する | 採用 | 「非目的」でBrowserのTOCTOUを受容しつつ「目的」で無条件に「閉じる」と書く矛盾は実際にある |
| B節: `allow_private_hosts`の型検証とhostname正規化 | 採用 | `terminal_failure_kinds`(`feedian/vault.py:529-531`)と同じ形の「JSON array of stringのみ許可・trim・重複除去」検証が要る。`config.fetch`は自由形式dictであるため、文字列1個を渡すと`frozenset(value)`が文字ごとに分解される具体的な事故が起こり得る |
| B節: `BrowserCandidate`が`allowed_private_hosts`単体ではなく`policy: FetchPolicy`を保持する | 採用 | レビュー1指摘3への対応をより具体化する。設定が今後増えたときの配線漏れを構造的に防げる |
| C節: 「Git tagに固定」ではなく「trusted Git repositoryを信頼点とする」に言い換え、脅威モデルを一文明記する | 採用 | tagは書き換え可能な参照であり、署名もされていない。正確な表現に直す |
| C節: `download_and_restore`がtagをfetchしてから`git show`する | 採用 | `feedian/restore.py:50-65`を確認したところ、`download_and_restore`は`git fetch`を一切呼んでいない。fresh clone直後や、別マシンで作成された新しいtagがローカルに存在しないケースで`git show <tag>:...`が失敗する。`restore_database`(ローカルtag必須)と`download_and_restore`(originからtagを確保してから`restore_database`を呼ぶ)で責務を分ける案を採る |
| D節: 「Feedian全体」を「現行Vault configのboolean設定」に限定する | 採用 | 実際に直すのは`ProviderSettings.enabled`と`RssFeedSettings.enabled`の2箇所のみであり、「全体」は言い過ぎである |
| E節 | 採用(変更なし) | |
| 未決事項: `timeout_seconds`/`browser_timeout_seconds`を`FetchPolicy`へ統合する | 採用 | 「1回のfetchをどう安全に行うか」と「失敗したfetchをいつ再実行するか」で責務を分ける区分は妥当。`FetchRetrySettings`とは別に保つ |
| 未決事項: proxyを今回無効化する | 採用 | 追加指摘2と同じ理由 |

#### 設計への影響のまとめ

レビュー2を経て、A節とB節の設計は次のように変わる。

- **対象範囲がpage fetchとRSS fetchの両方になる。** `extract.py`か独立モジュールに`ValidatingHTTPHandler`/`ValidatingHTTPSHandler`(またはconnectionファクトリ)を置き、`fetch_page_text`と`fetch_rss_items`の両方がこれを使う共通のopener構築関数(例: `build_fetch_opener(policy)`)を経由する。
- **接続の検証済み化は`_create_connection`の差し替えで実現する。** `connect()`自体は上書きしない。
- **検証は「全addrinfoを検査し、その集合の中だけで接続を試みる」形にする。** 単一IPを返す設計は採らない。
- **proxyは明示的に無効化する。** `ProxyHandler({})`を`build_opener`へ渡し、環境変数によるproxy自動利用を止める。
- **`allow_private_hosts`は型検証とhostname正規化(trim・小文字化・空文字拒否・重複除去)を経てから`frozenset[str]`になる。**
- **`BrowserCandidate`は`allowed_private_hosts`ではなく`policy: FetchPolicy`を保持する。**
- **C節は`restore_database(archive, tag)`(ローカルtag必須)と`download_and_restore(vault_root, tag)`(originからtagを確保してから`restore_database`を呼ぶ)の2層に分ける。** 脅威モデルの一文を追加する。

#### 未決事項への回答

草案の未決事項3件はすべて本ラウンドまでで解消した。

| # | 論点 | 結論 |
|---|---|---|
| 1 | リダイレクト先ピン留めの実現方法 | `_create_connection`差し替え方式により、ホップごとに新しい接続が自然に検証されるため、追加の実現方法は不要 |
| 2 | `timeout_seconds`/`browser_timeout_seconds`を`FetchPolicy`へ統合するか | 統合する。「1回のfetchの安全な実行」という責務に含める |
| 3 | tag未指定時の後方互換性 | 残さない。ただし`download_and_restore`はoriginからtagをfetchしてから検証する経路を持つため、「ローカルにtagがない」ことと「復元できない」ことは同義にならない |

新たな未決事項は、proxy経由での接続を今回完全に禁止することが利用者にとって受け入れ可能かどうかである。ドキュメント化された機能ではないため実装は進めてよいが、最終案への注記として要件所有者の確認を残す。


### レビュー4 — ChatGPT (2026-08-20)

結論は **軽微な修正後に確定可**。レビュー1〜3で主要な設計上の欠落は解消された。新たに重大なセキュリティ欠陥は見つからなかった。ただし、共通transport化に伴う設定責務の混同と、CPython内部インターフェースへの依存について、最終案で明示すべき点が2つある。

#### 指摘1：RSSへ`FetchPolicy`を丸ごと渡すと既存挙動を変える — 重大度: 中

レビュー3ではpage fetchとRSS fetchの両方が共通の安全なtransportを使用する設計になった。この方向自体は正しい。

しかし現在、

* Vaultの`fetch.timeout_seconds`既定値は **5秒**。
* `fetch_rss_items()`の`timeout_seconds`既定値は **30秒**。
* RSS XMLには`FEED_XML_MAX_BYTES = 10 MiB`という独立した上限がある。

という別々の意味を持っている。

したがって、

```python
fetch_rss_items(..., policy=FetchPolicy(...))
```

としてRSS側が`FetchPolicy.timeout_seconds`や`document_max_bytes`までそのまま使用すると、

```text
RSS timeout 30秒 → 5秒
RSS XML上限 10 MiB → document_max_bytes依存
```

となり得る。

これは草案の非目的である

> fetch設定のスキーマ自体の見直しは行わない。既存キーを実配線するだけ

と衝突する。

**採否: 修正して採用。**

共通化するのは「安全なネットワーク接続ポリシー」に限定した方がよい。

例えば概念上、

```python
@dataclass(frozen=True)
class NetworkPolicy:
    allowed_private_hosts: frozenset[str]
    use_proxy: bool = False

@dataclass(frozen=True)
class FetchPolicy:
    network: NetworkPolicy
    html_max_bytes: int
    document_max_bytes: int
    timeout_seconds: int
    browser_timeout_seconds: int
```

とする。

その上で、

```text
build_fetch_opener(NetworkPolicy)
        ↓
 page fetch       RSS fetch
     ↓               ↓
 FetchPolicy      RSS固有timeout=30
                  RSS XML limit=10MiB
```

とするのが責務上きれい。

必ずしもdataclassを2つに分ける必要はないが、**RSSは共通transportのSSRF/proxy設定だけを共有し、page-fetch固有のsize/timeout設定までは継承しない**ことを仕様に明記すべきである。

#### 指摘2：`_create_connection`は採用してよいが、CPython実装依存として固定する — 重大度: 低〜中

レビュー3で採用された、

> `connect()`を丸ごと上書きせず`self._create_connection`を差し替える

という方式は適切。

CPython 3.11、3.12、3.13では実際に`HTTPConnection.connect()`が

```python
self.sock = self._create_connection(
    (self.host, self.port),
    self.timeout,
    self.source_address,
)
```

を呼び、その後に`TCP_NODELAY`やtunnel処理を行う構造になっている。3.13の`HTTPSConnection.connect()`も`super().connect()`した後、元のhostまたはtunnel hostをSNIとしてTLS wrapする。

また、標準の`socket.create_connection()`はhostnameから`getaddrinfo()`を行い、得られた複数候補へ順に接続する関数なので、今回の差し替え関数はその振る舞いを「検証済みaddrinfo集合だけ」に限定して再現する必要がある。([Python documentation][1])

問題は、`_create_connection`が名前通り**公開APIではなくCPython内部属性**であること。

Feedianは現在Python 3.11〜3.13を対象としているので、この依存自体は許容可能だが、最終案に、

> `_create_connection`はCPythonの公開APIではないが、FeedianがサポートするPython 3.11/3.12/3.13で同一の接続フックとして存在することを確認済みとし、この実装契約をテストで固定する。

と記録しておくのがよい。

また差し替える関数は最低限、

```python
(address, timeout, source_address)
```

を互換に扱い、

```text
getaddrinfo
↓
全候補を検証
↓
1件でも禁止addressがあれば全体拒否
↓
検証済みaddrinfo集合だけで接続試行
↓
hostnameを再解決しない
```

を保証する。

これをテスト項目に追加すれば十分。

#### proxyの未決事項について

レビュー3末尾では、

> proxy経由での接続を今回完全に禁止することが利用者にとって受け入れ可能か

だけが未決として残っています。

これは**今回そのまま禁止で確定してよい**と思います。

理由は、現在のproxy利用がFeedianの明示的な機能ではなく、`urllib.build_opener()`が環境変数を暗黙に読むことで生じている挙動だからです。今回のSSRF保証を成立させるために、

```python
ProxyHandler({})
```

を明示する方が設計として一貫しています。

最終案には、

> HTTP/RSSコンテンツ取得では環境変数`HTTP_PROXY`/`HTTPS_PROXY`を使用しない。proxy対応はFeedianが安全性を定義できる明示的な設定方式を別仕様で導入するまで非対応とする。

くらい書いておけば十分です。

これはローカルagentにproxy環境変数を渡している処理とは別問題です。今回禁止するのは**Feedian自身のHTTP/RSS fetch transport**だけ、と範囲も明記しておくと誤解がありません。

#### 総括

この2点を取り込めば、**もう次は追加レビューを回すより「最終案」を書いて確定してよい段階**だと思います。

特に設計は、

```text
安全な共通Network transport
  ├─ HTTP page fetch
  └─ RSS feed fetch

Page FetchPolicy
  ├─ HTML上限
  ├─ document上限
  ├─ HTTP timeout
  └─ browser timeout

RSS
  ├─ RSS固有timeout
  └─ RSS XML固有上限
```

と整理するとかなり明快です。

AのDNS rebinding、Bの設定配線、Cのrestore trust chain、Dのstrict bool、Eのwarning修正については、**レビュー3までの結論をそのまま最終案へ採用して問題ない**です。

[1]: https://docs.python.org/3/library/socket.html?highlight=gethostbyname&utm_source=chatgpt.com "socket — Low-level networking interface — Python 3.14.5 documentation"

### レビュー5 — Claude Code (2026-08-20)

結論は**採用**である。引用された内容はいずれも実コードで再現を確認した。事実誤認はない。

| ChatGPTの指摘 | 採否 | 理由 |
|---|---|---|
| 指摘1: RSSへ`FetchPolicy`を丸ごと渡さず、共有するのは接続の安全性設定(`NetworkPolicy`相当)に限る | 採用 | `feedian/rss.py:22`の`FEED_XML_MAX_BYTES = 10 * 1024 * 1024`と`:34`の`timeout_seconds: int = 30`を確認した。どちらも`fetch.document_max_bytes`(既定100 MiB)・`fetch.timeout_seconds`(既定5秒)とは意味も既定値も異なる別概念であり、レビュー3の「共通のopener構築関数を経由する」という結論だけでは、この区別が仕様上どこにも書かれていなかった。見落としである |
| 指摘2: `_create_connection`への依存を、対象Pythonバージョンで確認済みの実装契約として明記する | 採用 | `HTTPConnection.connect()`/`HTTPSConnection.connect()`の構造はレビュー3で確認済みだが、確認に使ったインタプリタはPython 3.14.3であり、Feedianの対象バージョン(3.11/3.12/3.13、CI matrixで確認済み)そのものでの検証ではなかった。**この契約をテストで固定する**という指摘の対応方針を採る |
| proxyを今回禁止で確定する | 採用 | レビュー3が残した未決事項への回答として妥当。「Feedian自身のHTTP/RSS fetch transportだけを対象とし、ローカルagentへのproxy環境変数受け渡しとは別問題」という範囲の明記も併せて採る |

#### 設計への影響のまとめ

- 共通化する対象を`NetworkPolicy`(allowed_private_hosts・proxy無効化)に限定し、`FetchPolicy`はpage fetch固有のサイズ/timeout設定を保つ。RSS fetchは`NetworkPolicy`だけを共有し、`timeout_seconds=30`・`FEED_XML_MAX_BYTES`はRSS固有のまま変更しない。
- `_create_connection`差し替え方式の実装契約(全addrinfo検証・hostname再解決なし・`(address, timeout, source_address)`の互換性)を、対象3バージョン(3.11/3.12/3.13)それぞれでのテストとして固定する。
- proxy無効化(`ProxyHandler({})`)を確定し、対象範囲をFeedian自身のHTTP/RSS fetch transportに限定する一文を明記する。

#### 未決事項

なし。レビュー1〜5を通じて草案の未決事項3件と、レビュー3・4で新たに生じた論点(proxy扱い、RSSとpage fetchの設定分離、`_create_connection`の契約明記)はすべて解消した。次のステップは最終案の作成である。


### レビュー6 — ChatGPT (2026-08-20)

最新版は、**ほぼそのまま実装に入ってよい状態**です。A〜E、proxy非対応、RSS/pageの責務分離、CPython依存の扱いまで一貫しています。

ただし、**A節のサンプルコードに1点だけ仕様との不一致があります**。ここは実装前に直した方がいいです。

結論は **1点修正後、確定のままで実装可**。

#### 指摘1：`socket.create_connection(sockaddr[:2], ...)` が再度`getaddrinfo()`を呼ぶ — 重大度: 中

最終案では、

> 接続は検証済みaddrinfo集合の中だけで試み、hostnameを再解決しない

と正しく定義されています。

ところがサンプルの `_validated_create_connection()` は、

```python
infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)

...

for *_, sockaddr in infos:
    try:
        return socket.create_connection(
            sockaddr[:2], timeout, source_address
        )
```

となっています。

`socket.create_connection()` 自体が内部で改めて、

```python
for res in getaddrinfo(host, port, 0, SOCK_STREAM):
```

を実行します。CPython 3.13の実装でも明確にそうなっています。

ここで渡している`host`は元hostnameではなく数値IPなので、通常の意味での**DNS rebindingが復活するわけではありません**。したがってセキュリティ上の重大な穴ではありません。

ただし、

> 「同一のaddrinfo結果だけを使って接続する」

という仕様とは一致しません。

また、

```python
sockaddr[:2]
```

とすると、最初の`getaddrinfo()`で得た

```text
family
socktype
protocol
sockaddr
```

のうち、接続先IPとport以外を捨てています。

特にIPv6では`sockaddr`が、

```python
(address, port, flowinfo, scope_id)
```

になり得るので、**検証済みのaddrinfoをそのまま使う方が正しい**です。

#### 修正案

CPython自身の`socket.create_connection()`とほぼ同じ処理を行い、違いを

> `getaddrinfo()`を最初の1回しか行わない

ことにします。

```python
def _validated_create_connection(
    address,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address=None,
    *,
    allowed_private_hosts,
):
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)

    normalized_host = normalize_hostname(host)

    if normalized_host not in allowed_private_hosts:
        for family, socktype, proto, _, sockaddr in infos:
            candidate = ipaddress.ip_address(
                sockaddr[0].split("%", 1)[0]
            )
            if not candidate.is_global:
                raise ValueError(
                    f"non-public address is not allowed: {candidate}"
                )

    last_error = None

    for family, socktype, proto, _, sockaddr in infos:
        sock = None
        try:
            sock = socket.socket(family, socktype, proto)

            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)

            if source_address:
                sock.bind(source_address)

            sock.connect(sockaddr)
            return sock

        except OSError as exc:
            last_error = exc
            if sock is not None:
                sock.close()

    if last_error is not None:
        raise last_error

    raise OSError("getaddrinfo returned an empty list")
```

これなら、

```text
getaddrinfo(host)
        ↓ 1回だけ
全addrinfoをSSRF検証
        ↓
検証済み family/socktype/proto/sockaddr
        ↓
socket(...)
        ↓
connect(sockaddr)
```

となり、**最終案に書かれたセキュリティモデルと実装が完全に一致します**。

CPython自身も`getaddrinfo()`で取得した`af, socktype, proto, sa`からsocketを生成し、その`sa`へ直接`connect()`しています。

#### 検証項目も少しだけ変更

現在の、

> 2. hostnameは接続時に再解決されない

をもう少し具体化して、

> **2. `_validated_create_connection`内の`socket.getaddrinfo`は1回だけ呼ばれ、取得した`family`/`socktype`/`proto`/`sockaddr`を直接`socket.socket(...).connect(sockaddr)`へ使用する。`socket.create_connection`は呼ばない。**

としておけば十分です。

---

それ以外については、**今回新たなブロッカーは見つかりませんでした**。

特に最終案で、

```text
NetworkPolicy
     ↓
安全なHTTP transport
 ├ page fetch → FetchPolicy
 └ RSS fetch  → RSS固有timeout/limit
```

まで責務を分離したのは良い着地点です。また、Browserの残存リスク、proxy非対応、`_create_connection`という内部APIへの依存についても「受け入れる不正確さ」として明文化されているので、後から設計意図が分からなくなる問題もほぼ解消されています。

**上の`socket.create_connection()`だけ直接socket生成へ置き換えれば、「ステータス: 確定」のまま実装へ進めてよい**と判断します。

### レビュー7 — Claude Code (2026-08-20)

結論は**採用**である。指摘を実コードで再現を確認した。事実誤認はない。

| ChatGPTの指摘 | 採否 | 理由 |
|---|---|---|
| 指摘1: `_validated_create_connection`内の`socket.create_connection(sockaddr[:2], ...)`が`getaddrinfo`を再実行する | 採用 | 標準ライブラリの`socket.create_connection`のソースを確認したところ、`host, port = address`のあと`for res in getaddrinfo(host, port, 0, SOCK_STREAM):`を実行しており、指摘通り2回目の`getaddrinfo`呼び出しが発生する。渡しているのは数値IPであるためネットワーク越しのDNS問い合わせには発展せずセキュリティ上の重大な穴ではないが、最終案が明記した「検証済みaddrinfo集合の中だけで試み、hostnameを再解決しない」という前提と実装が一致しておらず、修正が要る。あわせて`sockaddr[:2]`によるIPv6の`flowinfo`/`scope_id`切り捨ても実際の不正確さであり、修正案(`family`/`socktype`/`proto`/`sockaddr`を直接使って`socket.socket(...)`→`connect(sockaddr)`する)を採る |

最終案A節のコード例と検証項目2を修正し、改訂1として記録する。
