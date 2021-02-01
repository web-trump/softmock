import asyncio
import hashlib
import json
import logging
import os.path
import sqlite3
import re
import base64
from io import BytesIO
from typing import ClassVar, Optional
from pyparsing import Keyword
from urllib import parse
from cchardet import detect

import tornado.escape
import tornado.web
import tornado.websocket

import mitmproxy.flow
import mitmproxy.tools.web.master  # noqa
from mitmproxy import contentviews
from mitmproxy import exceptions
from mitmproxy import flowfilter
from mitmproxy import http
from mitmproxy import io
from mitmproxy import log
from mitmproxy import optmanager
from mitmproxy import version
from mitmproxy import ctx
from .replay import proxy_req


def flow_to_json(flow: mitmproxy.flow.Flow) -> dict:
    """
    Remove flow message content and cert to save transmission space.

    Args:
        flow: The original flow.
    """
    f = {
        "id": flow.id,
        "intercepted": flow.intercepted,
        "is_replay": flow.is_replay,
        "client_conn": flow.client_conn.get_state(),
        "server_conn": flow.server_conn.get_state(),
        "type": flow.type,
        "modified": flow.modified(),
        "marked": flow.marked,
    }
    # .alpn_proto_negotiated is bytes, we need to decode that.
    for conn in "client_conn", "server_conn":
        if f[conn]["alpn_proto_negotiated"] is None:
            continue
        f[conn]["alpn_proto_negotiated"] = \
            f[conn]["alpn_proto_negotiated"].decode(errors="backslashreplace")
    # There are some bytes in here as well, let's skip it until we have them in the UI.
    f["client_conn"].pop("tls_extensions", None)
    if flow.error:
        f["error"] = flow.error.get_state()

    if isinstance(flow, http.HTTPFlow):
        content_length: Optional[int]
        content_hash: Optional[str]
        if flow.request:
            if flow.request.raw_content:
                content_length = len(flow.request.raw_content)
                content_hash = hashlib.sha256(
                    flow.request.raw_content).hexdigest()
            else:
                content_length = None
                content_hash = None
            try:
                raw_content = flow.request.raw_content.decode()
            except:
                raw_content = ""
            f["request"] = {
                "method": flow.request.method,
                "scheme": flow.request.scheme,
                "host": flow.request.host,
                "port": flow.request.port,
                "path": flow.request.path,
                "raw_content": raw_content,
                "http_version": flow.request.http_version,
                "headers": tuple(flow.request.headers.items(True)),
                "contentLength": content_length,
                "contentHash": content_hash,
                "timestamp_start": flow.request.timestamp_start,
                "timestamp_end": flow.request.timestamp_end,
                # TODO: remove, use flow.is_replay instead.
                "is_replay": flow.is_replay == "request",
                "pretty_host": flow.request.pretty_host,
            }
        if flow.response:
            if flow.response.raw_content:
                content_length = len(flow.response.raw_content)
                content_hash = hashlib.sha256(
                    flow.response.raw_content).hexdigest()
                # 获取html
                try:
                    html = flow.response.content.decode()
                except:
                    html = None
            else:
                content_length = None
                content_hash = None
                html = None

            f["response"] = {
                "http_version": flow.response.http_version,
                "status_code": flow.response.status_code,
                "reason": flow.response.reason,
                "headers": tuple(flow.response.headers.items(True)),
                "contentLength": content_length,
                "contentHash": content_hash,
                "timestamp_start": flow.response.timestamp_start,
                "timestamp_end": flow.response.timestamp_end,
                # TODO: remove, use flow.is_replay instead.
                "is_replay": flow.is_replay == "response",
                # 内容
                "html": html
            }
            if flow.response.data.trailers:
                f["response"]["trailers"] = tuple(
                    flow.response.data.trailers.items(True))

    f.get("server_conn", {}).pop("certificate_list", None)
    f.get("client_conn", {}).pop("certificate_list", None)
    f.get("client_conn", {}).pop("mitmcert", None)
    f.get("server_conn", {}).pop("alpn_offers", None)
    f.get("client_conn", {}).pop("alpn_offers", None)

    return f


def logentry_to_json(e: log.LogEntry) -> dict:
    return {
        "id": id(e),  # we just need some kind of id.
        "message": e.msg,
        "level": e.level
    }


class APIError(tornado.web.HTTPError):
    pass


class RequestHandler(tornado.web.RequestHandler):
    application: "Application"

    def write(self, chunk):
        # Writing arrays on the top level is ok nowadays.
        # http://flask.pocoo.org/docs/0.11/security/#json-security
        if isinstance(chunk, list):
            chunk = tornado.escape.json_encode(chunk)
            self.set_header("Content-Type", "application/json; charset=UTF-8")
        super().write(chunk)

    def set_default_headers(self):
        super().set_default_headers()
        self.set_header("Server", version.MITMPROXY)
        # 允许跨域
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Headers", "x-requested-with")
        self.set_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.set_header("X-Frame-Options", "DENY")
        self.add_header("X-XSS-Protection", "1; mode=block")
        self.add_header("X-Content-Type-Options", "nosniff")
        self.add_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "connect-src 'self' ws:; "
            "style-src   'self' 'unsafe-inline'"
        )

    @property
    def json(self):
        if not self.request.headers.get("Content-Type", "").startswith("application/json"):
            raise APIError(
                400, "Invalid Content-Type, expected application/json.")
        try:
            return json.loads(self.request.body.decode())
        except Exception as e:
            raise APIError(400, "Malformed JSON: {}".format(str(e)))

    @property
    def filecontents(self):
        """
        Accept either a multipart/form file upload or just take the plain request body.

        """
        if self.request.files:
            return next(iter(self.request.files.values()))[0].body
        else:
            return self.request.body

    @property
    def view(self) -> "mitmproxy.addons.view.View":
        return self.application.master.view

    @property
    def master(self) -> "mitmproxy.tools.web.master.WebMaster":
        return self.application.master

    @property
    def flow(self) -> mitmproxy.flow.Flow:
        flow_id = str(self.path_kwargs["flow_id"])
        # FIXME: Add a facility to addon.view to safely access the store
        flow = self.view.get_by_id(flow_id)
        if flow:
            return flow
        else:
            raise APIError(404, "Flow not found.")

    def write_error(self, status_code: int, **kwargs):
        if "exc_info" in kwargs and isinstance(kwargs["exc_info"][1], APIError):
            self.finish(kwargs["exc_info"][1].log_message)
        else:
            super().write_error(status_code, **kwargs)


class IndexHandler(RequestHandler):
    def get(self):
        token = self.xsrf_token  # https://github.com/tornadoweb/tornado/issues/645
        assert token
        host_filter = self.get_argument('host_filter')
        if ctx.options.host != host_filter and ctx.options.host:
            print('host更换为：', host_filter)
        ctx.options.host = host_filter
        self.render("index.html")


class Manifest(RequestHandler):
    def get(self):
        self.render("manifest.json")


class FilterHelp(RequestHandler):
    def get(self):
        self.write(dict(
            commands=flowfilter.help
        ))


class WebSocketEventBroadcaster(tornado.websocket.WebSocketHandler):
    # raise an error if inherited class doesn't specify its own instance.
    connections: ClassVar[set]

    def open(self):
        self.connections.add(self)

    def on_close(self):
        self.connections.remove(self)

    def set_default_headers(self):
        super().set_default_headers()
        self.set_header("Server", version.MITMPROXY)
        # 允许跨域
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Headers", "x-requested-with")
        self.set_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        # self.set_header("X-Frame-Options", "DENY")
        # self.add_header("X-XSS-Protection", "1; mode=block")
        # self.add_header("X-Content-Type-Options", "nosniff")
        # self.add_header(
        #     "Content-Security-Policy",
        #     "default-src 'self'; "
        #     "connect-src 'self' ws:; "
        #     "style-src   'self' 'unsafe-inline'"
        # )

    @classmethod
    def broadcast(cls, **kwargs):
        if kwargs['resource'] != "flows":
            return
        try:
            if not ctx.options.host in kwargs['data']['request']['host']:
                return
        except:
            return

        # 记录数据库
        msg_id = kwargs['data']['id']  # 消息id
        msg_type = kwargs['cmd']  # 记录到数据库的类型
        req = kwargs['data']['request']
        is_update_response = False if not kwargs['data'].get(
            'response', None) else True if kwargs['data']['response'].get('html', None) else False
        url = req['scheme'] + '://' + req['host'] + req['path'].split('?')[0]
        db = sqlite3.connect("soft_mock.db")
        cursor = db.cursor()
        sql = f"select count(*) from Mock1 where url='{url}'"
        if [i for i in cursor.execute(sql)][0][0]:  # 已经存在记录，更新记录
            """
            已经存在记录，则不需要返回新的id，直接把旧的id返回去
            """
            sql = f"select `detail` from Mock1 where url='{url}'"
            js = [i for i in cursor.execute(sql)][0][0]
            result = json.loads(parse.unquote(js))
            kwargs['data']['id'] = result['data']['id']
            kwargs['cmd'] = 'update'
            if not is_update_response:
                kwargs['data']['response'] = result['data'].get(
                    'response', None)
            message = parse.quote(json.dumps(kwargs, ensure_ascii=False))
            sql = f"update Mock1 set `detail`=? where url=?"
            cursor.execute(sql, (message, url))
        else:  # 新增记录
            message = parse.quote(json.dumps(kwargs, ensure_ascii=False))
            sql = f"insert into Mock1 (`id`, `detail`, `url`, `status`) values (?, ?, ?, ?)"
            cursor.execute(
                sql, (msg_id, message, url, '1'))

        db.commit()
        cursor.close()
        db.close()

        for conn in cls.connections:
            try:
                conn.write_message(parse.unquote(message))
            except Exception:  # pragma: no cover
                # logging.error("Error sending message", exc_info=True)
                pass


class ClientConnection(WebSocketEventBroadcaster):
    connections: ClassVar[set] = set()


class Flows(RequestHandler):
    def get(self):
        # result = [flow_to_json(f) for f in self.view]
        # print('result', result)
        # 获取历史记录
        db = sqlite3.connect("soft_mock.db")
        cursor = db.cursor()
        sql = f"select * from Mock1 where url like '%{ctx.options.host}%'"
        result = [{**json.loads(parse.unquote(i[1]))['data'], "status": parse.unquote(i[3])}
                  for i in cursor.execute(sql)]
        self.write(result)
        cursor.close()
        db.close()


class DumpFlows(RequestHandler):
    def get(self):
        self.set_header("Content-Disposition", "attachment; filename=flows")
        self.set_header("Content-Type", "application/octet-stream")

        bio = BytesIO()
        fw = io.FlowWriter(bio)
        for f in self.view:
            fw.add(f)

        self.write(bio.getvalue())
        bio.close()

    def post(self):
        self.view.clear()
        bio = BytesIO(self.filecontents)
        for i in io.FlowReader(bio).stream():
            asyncio.ensure_future(self.master.load_flow(i))
        bio.close()


class ClearAll(RequestHandler):
    def post(self):
        self.view.clear()
        self.master.events.clear()


class SOFTMOCK_ClearAll(RequestHandler):
    def post(self):
        db = sqlite3.connect("soft_mock.db")
        cursor = db.cursor()
        sql = f"delete from Mock1 where url like '%{ctx.options.host}%'"

        cursor.execute(sql)
        db.commit()
        cursor.close()
        db.close()
        self.write('0')


class ResumeFlows(RequestHandler):
    def post(self):
        for f in self.view:
            f.resume()
            self.view.update([f])


class KillFlows(RequestHandler):
    def post(self):
        for f in self.view:
            if f.killable:
                f.kill()
                self.view.update([f])


class ResumeFlow(RequestHandler):
    def post(self, flow_id):
        self.flow.resume()
        self.view.update([self.flow])


class KillFlow(RequestHandler):
    def post(self, flow_id):
        if self.flow.killable:
            self.flow.kill()
            self.view.update([self.flow])


class DeleteFlow(RequestHandler):
    def post(self):
        '''
        删除记录
        '''
        url = base64.b64decode(self.get_argument('url').encode()).decode()
        db = sqlite3.connect("soft_mock.db")
        cursor = db.cursor()
        sql = f"delete from Mock1 where `url`='{url}'"
        cursor.execute(sql)
        db.commit()
        cursor.close()
        db.close()
        self.write('0')


class CreateFlow(RequestHandler):
    def post(self):
        '''
        新增记录
        '''
        url = base64.b64decode(self.get_argument('url').encode()).decode()
        detail = self.request.body.decode()
        db = sqlite3.connect("soft_mock.db")
        cursor = db.cursor()
        sql = f"insert into Mock1 (`detail`, `status`, `url`) values ('{parse.quote(detail)}', '1', '{url}')"
        cursor.execute(sql)
        db.commit()
        cursor.close()
        db.close()
        self.write('0')


class UpdateFlow(RequestHandler):
    def post(self):
        '''
        更新在网页端修改的值
        '''
        url = base64.b64decode(self.get_argument('url').encode()).decode()
        status = self.get_argument('status')
        detail = self.request.body.decode()
        db = sqlite3.connect("soft_mock.db")
        cursor = db.cursor()
        sql = f"update Mock1 set `detail`='{parse.quote(detail)}', `status`='{status}' where `url`='{url}'"
        cursor.execute(sql)
        db.commit()
        cursor.close()
        db.close()
        self.write('0')


class UpdateStatus():
    def post(self):
        '''
        更新监听状态
        '''
        url = base64.b64decode(self.get_argument('url').encode()).decode()
        status = self.get_argument('status')
        db = sqlite3.connect("soft_mock.db")
        cursor = db.cursor()
        sql = f"update Mock1 set status='{status}' where url='{url}'"
        cursor.execute(sql)
        db.commit()
        cursor.close()
        self.write('0')


class FlowHandler(RequestHandler):
    def delete(self, flow_id):
        if self.flow.killable:
            self.flow.kill()
        self.view.remove([self.flow])

    def put(self, flow_id):
        flow = self.flow
        flow.backup()
        try:
            for a, b in self.json.items():
                if a == "request" and hasattr(flow, "request"):
                    request = flow.request
                    for k, v in b.items():
                        if k in ["method", "scheme", "host", "path", "http_version"]:
                            setattr(request, k, str(v))
                        elif k == "port":
                            request.port = int(v)
                        elif k == "headers":
                            request.headers.clear()
                            for header in v:
                                request.headers.add(*header)
                        elif k == "trailers":
                            request.trailers.clear()
                            for trailer in v:
                                request.trailers.add(*trailer)
                        elif k == "content":
                            request.text = v
                        else:
                            raise APIError(
                                400, f"Unknown update request.{k}: {v}")

                elif a == "response" and hasattr(flow, "response"):
                    response = flow.response
                    for k, v in b.items():
                        if k in ["msg", "http_version"]:
                            setattr(response, k, str(v))
                        elif k == "code":
                            response.status_code = int(v)
                        elif k == "headers":
                            response.headers.clear()
                            for header in v:
                                response.headers.add(*header)
                        elif k == "trailers":
                            response.trailers.clear()
                            for trailer in v:
                                response.trailers.add(*trailer)
                        elif k == "content":
                            response.text = v
                        else:
                            raise APIError(
                                400, f"Unknown update response.{k}: {v}")
                else:
                    raise APIError(400, f"Unknown update {a}: {b}")
        except APIError:
            flow.revert()
            raise
        self.view.update([flow])


class DuplicateFlow(RequestHandler):
    def post(self, flow_id):
        f = self.flow.copy()
        self.view.add([f])
        self.write(f.id)


class RevertFlow(RequestHandler):
    def post(self, flow_id):
        if self.flow.modified():
            self.flow.revert()
            self.view.update([self.flow])


class ReplayFlow(RequestHandler):
    def post(self, flow_id):
        self.flow.backup()
        self.flow.response = None
        self.view.update([self.flow])

        try:
            self.master.commands.call("replay.client", [self.flow])
        except exceptions.ReplayException as e:
            raise APIError(400, str(e))


class ReplayMe(RequestHandler):
    def post(self):
        url = base64.b64decode(self.get_argument('url').encode()).decode()

        db = sqlite3.connect("soft_mock.db")
        cursor = db.cursor()
        sql = f"select `detail` from Mock1 where url='{url}'"
        js = [i for i in cursor.execute(sql)][0][0]
        result = json.loads(parse.unquote(js))
        # result['data']['response'] = None
        update_result = proxy_req(result, self.get_argument('url'))
        sql2 = f"update Mock1 set `detail`='{parse.quote(json.dumps(update_result))}' where url='{url}'"
        cursor.execute(sql2)
        db.commit()
        cursor.close()
        self.write('0')


class FlowContent(RequestHandler):
    def post(self, flow_id, message):
        self.flow.backup()
        message = getattr(self.flow, message)
        message.content = self.filecontents
        self.view.update([self.flow])

    def get(self, flow_id, message):
        message = getattr(self.flow, message)

        if not message.raw_content:
            raise APIError(400, "No content.")

        content_encoding = message.headers.get("Content-Encoding", None)
        if content_encoding:
            content_encoding = re.sub(r"[^\w]", "", content_encoding)
            self.set_header("Content-Encoding", content_encoding)

        original_cd = message.headers.get("Content-Disposition", None)
        filename = None
        if original_cd:
            filename = re.search(r'filename=([-\w" .()]+)', original_cd)
            if filename:
                filename = filename.group(1)
        if not filename:
            filename = self.flow.request.path.split("?")[0].split("/")[-1]

        filename = re.sub(r'[^-\w" .()]', "", filename)
        cd = f"attachment; filename={filename}"
        self.set_header("Content-Disposition", cd)
        self.set_header("Content-Type", "application/text")
        self.set_header("X-Content-Type-Options", "nosniff")
        self.set_header("X-Frame-Options", "DENY")
        self.write(message.raw_content)


class FlowContentView(RequestHandler):
    def get(self, flow_id, message, content_view):
        message = getattr(self.flow, message)
        description, lines, error = contentviews.get_message_content_view(
            content_view.replace('_', ' '), message, self.flow
        )
        #        if error:
        #           add event log

        self.write(dict(
            lines=list(lines),
            description=description
        ))


class Events(RequestHandler):
    def get(self):
        self.write([logentry_to_json(e) for e in self.master.events.data])


class Settings(RequestHandler):
    def get(self):
        self.write(dict(
            version=version.VERSION,
            mode=str(self.master.options.mode),
            intercept_active=self.master.options.intercept_active,
            intercept=self.master.options.intercept,
            showhost=self.master.options.showhost,
            upstream_cert=self.master.options.upstream_cert,
            rawtcp=self.master.options.rawtcp,
            http2=self.master.options.http2,
            websocket=self.master.options.websocket,
            anticache=self.master.options.anticache,
            anticomp=self.master.options.anticomp,
            stickyauth=self.master.options.stickyauth,
            stickycookie=self.master.options.stickycookie,
            stream=self.master.options.stream_large_bodies,
            contentViews=[v.name.replace(' ', '_')
                          for v in contentviews.views],
            listen_host=self.master.options.listen_host,
            listen_port=self.master.options.listen_port,
            server=self.master.options.server,
        ))

    def put(self):
        update = self.json
        allowed_options = {
            "intercept", "showhost", "upstream_cert", "ssl_insecure",
            "rawtcp", "http2", "websocket", "anticache", "anticomp",
            "stickycookie", "stickyauth", "stream_large_bodies"
        }
        for k in update:
            if k not in allowed_options:
                raise APIError(400, f"Unknown setting {k}")
        self.master.options.update(**update)


class Options(RequestHandler):
    def get(self):
        self.write(optmanager.dump_dicts(self.master.options))

    def put(self):
        update = self.json
        try:
            self.master.options.update(**update)
        except Exception as err:
            raise APIError(400, f"{err}")


class SaveOptions(RequestHandler):
    def post(self):
        # try:
        #     optmanager.save(self.master.options, CONFIG_PATH, True)
        # except Exception as err:
        #     raise APIError(400, "{}".format(err))
        pass


class DnsRebind(RequestHandler):
    def get(self):
        raise tornado.web.HTTPError(
            403,
            reason="To protect against DNS rebinding, mitmweb can only be accessed by IP at the moment. "
                   "(https://github.com/mitmproxy/mitmproxy/issues/3234)"
        )


class Application(tornado.web.Application):
    master: "mitmproxy.tools.web.master.WebMaster"

    def __init__(self, master: "mitmproxy.tools.web.master.WebMaster", debug: bool) -> None:
        self.master = master
        super().__init__(
            default_host="dns-rebind-protection",
            template_path=os.path.join(os.path.dirname(__file__), "templates"),
            static_path=os.path.join(os.path.dirname(__file__), "static"),
            xsrf_cookies=False,
            cookie_secret=os.urandom(256),
            debug=False,
            autoreload=False,
        )

        self.add_handlers("dns-rebind-protection", [(r"/.*", DnsRebind)])
        self.add_handlers(
            # make mitmweb accessible by IP only to prevent DNS rebinding.
            r'^(localhost|[0-9.]+|\[[0-9a-fA-F:]+\])$',
            [
                (r"/", IndexHandler),
                (r"/manifest.json", Manifest),
                (r"/filter-help(?:\.json)?", FilterHelp),
                (r"/updates", ClientConnection),
                (r"/events(?:\.json)?", Events),
                (r"/flows(?:\.json)?", Flows),
                (r"/flows/dump", DumpFlows),
                (r"/flows/resume", ResumeFlows),
                (r"/create", CreateFlow),
                (r"/flows/kill", KillFlows),
                (r"/update_flow", UpdateFlow),
                (r"/delete_flow", DeleteFlow),
                (r"/clear_all", SOFTMOCK_ClearAll),
                (r"/replay", ReplayMe),
                (r"/update_status", UpdateStatus),
                (r"/flows/(?P<flow_id>[0-9a-f\-]+)", FlowHandler),
                (r"/flows/(?P<flow_id>[0-9a-f\-]+)/resume", ResumeFlow),
                (r"/flows/(?P<flow_id>[0-9a-f\-]+)/kill", KillFlow),
                (r"/flows/(?P<flow_id>[0-9a-f\-]+)/duplicate", DuplicateFlow),
                (r"/flows/(?P<flow_id>[0-9a-f\-]+)/replay", ReplayFlow),
                (r"/flows/(?P<flow_id>[0-9a-f\-]+)/revert", RevertFlow),
                (r"/flows/(?P<flow_id>[0-9a-f\-]+)/(?P<message>request|response)/content.data", FlowContent),
                (
                    r"/flows/(?P<flow_id>[0-9a-f\-]+)/(?P<message>request|response)/content/(?P<content_view>[0-9a-zA-Z\-\_]+)(?:\.json)?",
                    FlowContentView),
                (r"/settings(?:\.json)?", Settings),
                (r"/clear", ClearAll),
                (r"/options(?:\.json)?", Options),
                (r"/options/save", SaveOptions)
            ]
        )
