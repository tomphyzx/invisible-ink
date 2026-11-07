"""A tiny PDF writer: the report page, and test files with exact control over hidden-text tricks.

Standard library only. Pages use the built-in Helvetica font, so nothing is embedded.
Text outside Latin-1 is written as "?".

    doc = Doc()
    p = doc.page()                     # US Letter, 612 x 792 pt
    p.text(72, 700, 12, "Visible")     # x, y from the bottom-left, size in pt
    p.text(72, 680, 12, "Hidden", rgb=(1, 1, 1))
    p.rect(60, 600, 300, 40, rgb=(1, 1, 1))   # a filled box (draw it after text to cover it)
    p.image(60, 400, 200, 100, img)    # a PIL image
    doc.save("out.pdf")
"""

import zlib


def _esc(s):
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class Page:
    def __init__(self, doc, w, h):
        self.doc, self.w, self.h = doc, w, h
        self.ops, self.images, self.ocgs = [], [], []

    def text(self, x, y, size, s, rgb=(0, 0, 0), mode=0, layer=None):
        """mode 3 is PDF's own 'invisible' text rendering mode. layer: an OCG from doc.layer()."""
        op = "BT /F1 %g Tf %g %g %g rg %d Tr %g %g Td (%s) Tj ET" % (size, rgb[0], rgb[1], rgb[2], mode, x, y, _esc(s))
        if layer is not None:
            if layer not in self.ocgs:
                self.ocgs.append(layer)
            op = "/OC /OC%d BDC %s EMC" % (layer, op)
        self.ops.append(op)

    def rect(self, x, y, w, h, rgb=(1, 1, 1)):
        self.ops.append("%g %g %g rg %g %g %g %g re f" % (rgb[0], rgb[1], rgb[2], x, y, w, h))

    def clip_text(self, x, y, size, s):
        """Text drawn inside an empty clipping box, so none of it shows."""
        self.ops.append("q 0 0 1 1 re W n BT /F1 %g Tf 0 0 0 rg %g %g Td (%s) Tj ET Q" % (size, x, y, _esc(s)))

    def image(self, x, y, w, h, img):
        n = len(self.images)
        self.images.append(img.convert("RGB"))
        self.ops.append("q %g 0 0 %g %g %g cm /Im%d Do Q" % (w, h, x, y, n))


class Doc:
    def __init__(self, info=None):
        self.pages, self.info, self.layers = [], info or {}, 0

    def page(self, w=612, h=792):
        p = Page(self, w, h)
        self.pages.append(p)
        return p

    def layer(self):
        """An optional-content group (layer) that is switched OFF when the file opens."""
        self.layers += 1
        return self.layers - 1

    def save(self, path, extra_catalog=""):
        objs = []                                  # index i holds object number i + 1

        def add(body):
            objs.append(body)
            return len(objs)

        def stream(dict_part, data):
            return b"<< " + dict_part.encode() + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"

        font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        ocg_ids = [add(b"<< /Type /OCG /Name (Layer %d) >>" % i) for i in range(self.layers)]
        pages_id = len(objs) + 1 + sum(2 + len(p.images) for p in self.pages)   # reserved below
        kids = []
        for p in self.pages:
            xobj = []
            for k, img in enumerate(p.images):
                data = zlib.compress(img.tobytes())
                iid = add(stream("/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
                                 "/BitsPerComponent 8 /Filter /FlateDecode" % img.size, data))
                xobj.append("/Im%d %d 0 R" % (k, iid))
            content = add(stream("", "\n".join(p.ops).encode("latin-1", "replace")))
            props = " ".join("/OC%d %d 0 R" % (g, ocg_ids[g]) for g in p.ocgs)
            res = "<< /Font << /F1 %d 0 R >> /XObject << %s >> /Properties << %s >> >>" % (font, " ".join(xobj), props)
            kids.append(add(("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %g %g] /Resources %s /Contents %d 0 R >>"
                             % (pages_id, p.w, p.h, res, content)).encode()))
        assert len(objs) + 1 == pages_id
        add(("<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join("%d 0 R" % k for k in kids), len(kids))).encode())
        oc = ""
        if ocg_ids:
            refs = " ".join("%d 0 R" % i for i in ocg_ids)
            oc = "/OCProperties << /OCGs [%s] /D << /OFF [%s] >> >>" % (refs, refs)
        catalog = add(("<< /Type /Catalog /Pages %d 0 R %s %s >>" % (pages_id, oc, extra_catalog)).encode())
        info = add(("<< %s >>" % " ".join("/%s (%s)" % (k, _esc(v)) for k, v in self.info.items()))
                   .encode("latin-1", "replace"))
        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objs):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % (i + 1) + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
        for off in offsets:
            out += b"%010d 00000 n \n" % off
        out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
            len(objs) + 1, catalog, info, xref)
        with open(path, "wb") as f:
            f.write(out)
