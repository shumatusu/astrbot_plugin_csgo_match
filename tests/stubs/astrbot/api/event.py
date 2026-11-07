from .message_components import Plain, Image

class MessageChain:
    def __init__(self, chain=None, **kw):
        self.chain = list(chain or [])
    def message(self, text):
        self.chain.append(Plain(text))
        return self

class _Result:
    def __init__(self, kind, payload):
        self.kind = kind
        self.payload = payload
    def __repr__(self):
        return f"{self.kind}:{self.payload!r}"

class AstrMessageEvent:
    def __init__(self, message_str="", umo="test:group:1"):
        self.message_str = message_str
        self.unified_msg_origin = umo
        self.role = "member"
    def plain_result(self, text):
        return _Result("plain", text)
    def image_result(self, path):
        return _Result("image", path)

class _Filter:
    def command(self, name, *a, **kw):
        def deco(fn):
            fn.__command__ = name
            return fn
        return deco
    def permission_type(self, *a, **kw):
        def deco(fn):
            return fn
        return deco

filter = _Filter()
