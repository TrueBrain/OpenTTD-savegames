import click
import io
import struct
import lzma
import zlib

from collections import defaultdict
from openttd_helpers import click_helper


class PlainFile:
    @staticmethod
    def open(f):
        return f


class ZLibFile:
    @staticmethod
    def open(f):
        return ZLibFile(f)

    def __init__(self, file):
        self.file = file
        self.decompressor = zlib.decompressobj()
        self.uncompressed = bytearray()

    def close(self):
        pass

    def read(self, amount):
        while len(self.uncompressed) < amount:
            new_data = self.file.read(8192)
            if len(new_data) == 0:
                break
            self.uncompressed += self.decompressor.decompress(new_data)

        data = self.uncompressed[0:amount]
        self.uncompressed = self.uncompressed[amount:]
        return data


UNCOMPRESS = {
    b"OTTN": PlainFile,
    b"OTTZ": ZLibFile,
    b"OTTX": lzma,
    # OTTD is not supported (it is only used for very old savegames)
}


def read_uint8(fp):
    return struct.unpack(">B", fp.read(1))[0]


def read_uint16(fp):
    return struct.unpack(">H", fp.read(2))[0]


def read_uint24(fp):
    return read_uint16(fp) << 8 | read_uint8(fp)


def read_uint32(fp):
    return struct.unpack(">I", fp.read(4))[0]


def read_gamma(fp):
    b = read_uint8(fp)
    if (b & 0x80) == 0:
        return (b & 0x7F, 1)
    elif (b & 0xC0) == 0x80:
        return ((b & 0x3F) << 8 | read_uint8(fp), 2)
    elif (b & 0xE0) == 0xC0:
        return ((b & 0x1F) << 16 | read_uint16(fp), 3)
    elif (b & 0xF0) == 0xE0:
        return ((b & 0x0F) << 24 | read_uint24(fp), 4)
    elif (b & 0xF8) == 0xF0:
        return ((b & 0x07) << 32 | read_uint32(fp), 5)
    else:
        raise Exception("read_gamma() failed: invalid encoding")


def read_str(fp):
    size = read_gamma(fp)[0]
    return fp.read(size).decode()


def analyze_chunk(tag, index, data, analysis):
    fp = io.BytesIO(data)

    if tag == b"MAPS":
        analysis["map-size"] = "%dx%d" % (read_uint32(fp), read_uint32(fp))

    if tag == b"NGRF":
        if "newgrf" not in analysis:
            analysis["newgrf-count"] = 0

        analysis["newgrf-count"] += 1

    if tag == b"AIPL":
        name = read_str(fp)
        if name:
            if "ai" not in analysis:
                analysis["ai-count"] = 0

            analysis["ai-count"] += 1

    if tag == b"GSDT":
        name = read_str(fp)
        if name:
            if "gs" not in analysis:
                analysis["gs-count"] = 0

            analysis["gs-count"] += 1


def analyze_savegame(fp, analysis):
    while True:
        tag = fp.read(4)

        if len(tag) == 0 or tag == b"\0\0\0\0":
            break
        if len(tag) != 4:
            raise Exception("Savegame contains garbage at end of file")

        type = read_uint8(fp)
        if (type & 0x0F) == 0x00:
            size = type << 20 | read_uint24(fp)
            data = fp.read(size)
            analyze_chunk(tag, -1, data, analysis)
        elif type in (1, 2):
            index = -1
            while True:
                size = read_gamma(fp)[0] - 1
                if size < 0:
                    break
                if type == 2:
                    index, index_size = read_gamma(fp)
                    size -= index_size
                else:
                    index += 1

                data = fp.read(size)
                analyze_chunk(tag, index, data, analysis)
        else:
            raise Exception(f"Invalid chunk type {type}")


@click_helper.command()
@click.argument("files", nargs=-1, type=click.Path(exists=True, file_okay=True, dir_okay=False))
def main(files):
    analysis_all = []
    keys = set()

    for filename in files:
        with open(filename, "rb") as fp:
            format = fp.read(4)
            savegame_version = read_uint16(fp)
            read_uint16(fp)

            analysis = {
                "filename": filename.split("/")[-1],
                "savegame-version": savegame_version,
                "compression": format,
            }

            decompressor = UNCOMPRESS.get(format)
            if decompressor is None:
                raise Exception("Unknown savegame compression")

            fp2 = decompressor.open(fp)
            analyze_savegame(fp2, analysis)

        analysis_all.append(analysis)
        keys.update(analysis.keys())

    for key in keys:
        if key == "filename":
            continue

        detect_type = None
        for analysis in analysis_all:
            if key not in analysis:
                continue
            t = type(analysis[key])
            if detect_type is None or detect_type == t:
                detect_type = t
            else:
                detect_type = None
                break

        if detect_type is None:
            raise Exception(f"Failed to detect common type for {key}; this is an implementation bug")

        if detect_type == int:
            default_value = 0
        elif detect_type == str:
            default_value = ""
        elif detect_type == bytes:
            default_value = b""
        else:
            raise Exception(f"No default value implemented for type {str(detect_type)}")

        last_value = None
        slot = None
        values = defaultdict(list)
        for analysis in sorted(analysis_all, key=lambda x: x.get(key, default_value)):
            if key not in analysis:
                value = "unknown"
            else:
                value = analysis[key]

            if last_value != value:
                slot = value
                last_value = value

            values[slot].append(analysis["filename"])

        with open(f"metadata/{key}.yaml", "w") as fp:
            fp.write(f"{key}:\n")

            for slot, filenames in values.items():
                fp.write(f"  {slot}:\n")
                for filename in filenames:
                    fp.write(f"  - {filename}\n")

        with open(f"docs/{key}.html", "w") as fp:
            fp.write("<html><body>\n")
            fp.write(f"<h1>By {key}</h1>\n")

            for slot, filenames in values.items():
                fp.write(f"<h2>{slot}</h2>\n<ul>\n")
                for filename in filenames:
                    fp.write(
                        f'<li><a href="https://github.com/TrueBrain/OpenTTD-savegames/raw/master/savegames/{filename}">{filename}</a></li>\n'
                    )
                fp.write("</ul>\n")

            fp.write("</body></html>\n")

    with open("docs/index.html", "w") as fp:
        fp.write("<html><body>\n")
        fp.write("<h1>OpenTTD Savegames</h1>")
        fp.write("<ul>\n")

        for key in sorted(keys):
            if key == "filename":
                continue
            fp.write(f'<li><a href="{key}.html">by {key}</a></li>\n')

        fp.write("</ul>\n")
        fp.write("</body></html>\n")


if __name__ == "__main__":
    main()
