class Plain:
    def __init__(self, text=""):
        self.text = text
    def __repr__(self):
        return f"Plain({self.text!r})"

class Image:
    def __init__(self, file="", **kw):
        self.file = file or kw.get("path", "")
    @classmethod
    def fromFileSystem(cls, path):
        return cls(file=path)
    def __repr__(self):
        return f"Image({self.file!r})"
