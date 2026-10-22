import json
import base64
import secrets
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from .protocol import parse_batch
from .proactive_send import IlinkResponseDiagnostics, sanitize_errmsg
from .session_health import ReceiveHealthResult


DEFAULT_CHANNEL_VERSION = '2.4.6'
DEFAULT_LONG_POLL_TIMEOUT_MS = 45_000
DEFAULT_BOT_AGENT = 'ChatAgentReceiveOnly/0.1'


def _encode_client_version(version):
    """Encode a semver channel version as iLink's uint32 header value.

    The official client sends ``major << 16 | minor << 8 | patch`` in
    ``iLink-App-ClientVersion`` while keeping the dotted version in
    ``base_info.channel_version``.  Preserve non-semver values for injected
    test transports and custom deployments.
    """
    parts = str(version).split('.')
    if len(parts) < 3:
        return str(version)
    try:
        major = int(parts[0])
        minor = int(parts[1])
        patch = int(parts[2].split('-', 1)[0])
    except (TypeError, ValueError):
        return str(version)
    return str(((major & 0xff) << 16) | ((minor & 0xff) << 8) | (patch & 0xff))


class RealIlinkTransport:
    """Minimal protocol adapter. Network is contacted only when a method is called."""
    BASE='https://ilinkai.weixin.qq.com'
    ALLOWED=('get_bot_qrcode','get_qrcode_status','getupdates','notify_start')
    def __init__(self,token=None,base_url=None,timeout=DEFAULT_LONG_POLL_TIMEOUT_MS,channel_version=DEFAULT_CHANNEL_VERSION,account_scope='default',opener=None,app_client_version=None):
        if not isinstance(account_scope,str) or not account_scope.strip(): raise ValueError('account_scope is required')
        if timeout <= 0: raise ValueError('timeout must be positive')
        self.token=token; self.base_url=(base_url or self.BASE).rstrip('/'); self.timeout=timeout/1000; self.channel_version=str(channel_version); self.app_client_version=str(app_client_version) if app_client_version is not None else _encode_client_version(self.channel_version); self.account_scope=account_scope; self._opener=opener or urlopen; self.calls=[]
    def _get(self,path,timeout=None):
        req=Request(self.base_url+path,method='GET',headers={'Accept':'application/json'})
        with self._opener(req,timeout=timeout or self.timeout) as response:return json.loads(response.read().decode())
    @staticmethod
    def _response_diagnostics(status, result):
        if not isinstance(result, dict):
            return IlinkResponseDiagnostics(http_status=status, response_keys=())
        return IlinkResponseDiagnostics(
            http_status=status,
            ret=result.get('ret'),
            errcode=result.get('errcode'),
            errmsg=sanitize_errmsg(result.get('errmsg')),
            response_keys=tuple(sorted(str(key) for key in result.keys())),
        )

    def _post_with_diagnostics(self,path,body,timeout=None):
        if not self.token: raise RuntimeError('iLink token is required for getupdates')
        data=json.dumps(body).encode(); uin=base64.b64encode(str(secrets.randbits(32)).encode()).decode()
        req=Request(self.base_url+path,data=data,method='POST',headers={'Content-Type':'application/json','AuthorizationType':'ilink_bot_token','Authorization':'Bearer '+self.token,'X-WECHAT-UIN':uin,'iLink-App-Id':'bot','iLink-App-ClientVersion':self.app_client_version})
        with self._opener(req,timeout=timeout or self.timeout) as response:
            status = getattr(response, 'status', None)
            if status is None:
                status = getattr(response, 'code', None)
            result = json.loads(response.read().decode())
        return result, self._response_diagnostics(status, result)

    def _post(self,path,body,timeout=None):
        return self._post_with_diagnostics(path, body, timeout)[0]
    def get_bot_qrcode(self,bot_type='3'):
        self.calls.append('get_bot_qrcode')
        return self._get('/ilink/bot/get_bot_qrcode?'+urlencode({'bot_type':bot_type}))
    def get_qrcode_status(self,qrcode):
        self.calls.append('get_qrcode_status')
        return self._get('/ilink/bot/get_qrcode_status?'+urlencode({'qrcode':qrcode}))
    def notify_start(self):
        """Mark the receive session online without sending a WeChat message."""
        self.calls.append('notify_start')
        return self._post(
            '/ilink/bot/msg/notifystart',
            {'base_info': {'channel_version': self.channel_version, 'bot_agent': DEFAULT_BOT_AGENT}},
        )
    def getupdates(self,cursor):
        self.calls.append('getupdates')
        return parse_batch(self._post('/ilink/bot/getupdates',{'get_updates_buf':cursor,'base_info':{'channel_version':self.channel_version,'bot_agent':DEFAULT_BOT_AGENT}}), self.account_scope)

    def getupdates_with_diagnostics(self, cursor):
        """Perform one receive-only poll and return bounded response facts.

        The raw response (including any message/context fields) remains inside
        this call and is not returned.  Callers may use the diagnostic result
        for health classification without persisting a token or transcript.
        """
        self.calls.append('getupdates_with_diagnostics')
        try:
            raw, diagnostics = self._post_with_diagnostics(
                '/ilink/bot/getupdates',
                {'get_updates_buf': cursor,
                 'base_info': {'channel_version': self.channel_version,
                               'bot_agent': DEFAULT_BOT_AGENT}},
            )
        except HTTPError as error:
            # A health probe must return bounded facts instead of making the
            # production runner treat a transport exception as a successful
            # empty poll.  Do not read the HTTP error body: it may contain
            # protocol data or sensitive diagnostics.
            diagnostics = IlinkResponseDiagnostics(
                http_status=getattr(error, 'code', None),
                errmsg=type(error).__name__,
            )
            return ReceiveHealthResult(diagnostics=diagnostics, exception=error)
        except (TimeoutError, ConnectionError, OSError, URLError, ValueError) as error:
            diagnostics = IlinkResponseDiagnostics(errmsg=type(error).__name__)
            return ReceiveHealthResult(diagnostics=diagnostics, exception=error)
        try:
            batch = parse_batch(raw, self.account_scope)
        except (AttributeError, TypeError, ValueError) as error:
            diagnostics = IlinkResponseDiagnostics(
                http_status=diagnostics.http_status,
                ret=diagnostics.ret,
                errcode=diagnostics.errcode,
                errmsg=type(error).__name__,
                response_keys=diagnostics.response_keys,
            )
            return ReceiveHealthResult(diagnostics=diagnostics, exception=error)
        sender_ids = {m.sender_fingerprint for m in batch.messages if m.sender_fingerprint}
        bot_ids = {m.bot_id for m in batch.messages if m.bot_id}
        observed_account = (
            next(iter(sender_ids)) if len(sender_ids) == 1 else "__MIXED__" if sender_ids else None
        )
        observed_bot = (
            next(iter(bot_ids)) if len(bot_ids) == 1 else "__MIXED__" if bot_ids else None
        )
        return ReceiveHealthResult(
            diagnostics=diagnostics,
            message_count=len(batch.messages),
            cursor_present=bool(batch.cursor),
            account_fingerprint=observed_account,
            bot_fingerprint=observed_bot,
        )
