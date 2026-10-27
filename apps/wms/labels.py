"""SSCC labels for handling units, as ZPL (Zebra printers) or a one-page 4x6 inch PDF (any printer).

The barcode is GS1-128: Code 128 set C with FNC1, then AI 00 and the 18-digit SSCC. Both formats are built
here without a PDF or barcode library; the PDF is plain vector rectangles and the built-in Helvetica font.
"""

from dataclasses import dataclass

from .sscc import human

# Code 128 bar/space module widths for symbol values 0-105; STOP (106) has a final 2-module bar.
PATTERNS = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 221312 231212 112232 122132 122231 "
    "113222 123122 123221 223211 221132 221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 "
    "212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 231113 231311 112133 112331 132131 "
    "113123 113321 133121 313121 211331 231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 "
    "314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 112412 122114 122411 142112 142211 "
    "241211 221114 413111 241112 134111 111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 "
    "214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 114131 311141 411131 211412 211214 "
    "211232"
).split()
STOP = "2331112"
START_C, FNC1 = 105, 102
QUIET_MODULES = 10


def gs1_128_values(ai_and_data: str) -> list[int]:
    """Symbol values for an all-numeric GS1 element string (even length), including the check symbol."""
    if not (ai_and_data.isdigit() and len(ai_and_data) % 2 == 0):
        raise ValueError("set C needs an even number of digits")
    values = [START_C, FNC1] + [int(ai_and_data[i : i + 2]) for i in range(0, len(ai_and_data), 2)]
    check = (values[0] + sum(v * i for i, v in enumerate(values[1:], start=1))) % 103
    return [*values, check]


def modules(values: list[int]) -> list[tuple[bool, int]]:
    """(is_bar, width) runs for the symbol, stop pattern included."""
    runs = []
    for pattern in [PATTERNS[v] for v in values] + [STOP]:
        for i, width in enumerate(pattern):
            runs.append((i % 2 == 0, int(width)))
    return runs


@dataclass(frozen=True, slots=True)
class LabelData:
    sscc: str
    client: str
    order_number: str
    ship_from: str
    ship_to: str
    contents: str
    weight_kg: str


def zpl(label: LabelData) -> str:
    def text(value: str) -> str:
        # ^FH lets _XX escapes through; strip the characters that would end or start a ZPL command.
        return value.replace("^", " ").replace("~", " ")

    return "\n".join(
        [
            "^XA^CI28^PW812^LL1218",
            f"^FO40,40^A0N,34,34^FDFrom: {text(label.ship_from)}^FS",
            f"^FO40,90^A0N,40,40^FDTo: {text(label.ship_to)}^FS",
            f"^FO40,160^A0N,34,34^FDClient: {text(label.client)}   Order: {text(label.order_number)}^FS",
            f"^FO40,210^A0N,30,30^FD{text(label.contents)}^FS",
            f"^FO40,255^A0N,30,30^FDWeight: {text(label.weight_kg)} kg^FS",
            "^FO40,320^GB732,3,3^FS",
            "^FO40,360^A0N,30,30^FDSSCC^FS",
            f"^FO60,410^BY4^BCN,300,N,N,N^FD>;>800{label.sscc}^FS",
            f"^FO60,740^A0N,44,44^FD{human(label.sscc)}^FS",
            "^XZ",
        ]
    )


def _pdf_text(value: str) -> str:
    safe = value.encode("latin-1", "replace").decode("latin-1")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf(label: LabelData) -> bytes:
    width, height = 288, 432  # 4 x 6 inches in points
    ops = ["0 g"]

    def line(x: float, y: float, size: int, value: str) -> None:
        ops.append(f"BT /F1 {size} Tf {x:.2f} {y:.2f} Td ({_pdf_text(value)}) Tj ET")

    line(18, 400, 11, f"From: {label.ship_from}")
    line(18, 380, 14, f"To: {label.ship_to}")
    line(18, 356, 11, f"Client: {label.client}   Order: {label.order_number}")
    line(18, 338, 10, label.contents[:60])
    line(18, 322, 10, f"Weight: {label.weight_kg} kg")
    ops.append("18 310 252 1 re f")
    line(18, 292, 10, "SSCC")

    runs = modules(gs1_128_values("00" + label.sscc))
    total = sum(w for _, w in runs) + 2 * QUIET_MODULES
    module = (width - 24) / total  # fill the width between 12 pt margins, quiet zones included
    x = 12 + QUIET_MODULES * module
    for is_bar, w in runs:
        if is_bar:
            ops.append(f"{x:.3f} 150 {w * module:.3f} 130 re f")
        x += w * module
    line(36, 126, 14, human(label.sscc))

    content = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>".encode(),
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
