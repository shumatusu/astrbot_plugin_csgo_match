class Context:
    def __init__(self):
        self.sent = []
        self.providers = {}          # id -> provider
        self.using_provider = None

    def get_provider_by_id(self, provider_id):
        return self.providers.get(provider_id)

    def get_using_provider(self, umo=None):
        return self.using_provider
    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))

class Star:
    def __init__(self, context):
        self.context = context
    async def html_render(self, tmpl, data, options=None, return_url=False):
        raise RuntimeError("stub: html_render unavailable")

def register(*a, **kw):
    def deco(cls):
        return cls
    return deco
