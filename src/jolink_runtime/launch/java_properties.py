"""Small UTF-8 Properties files for private Java process configuration."""


def write_properties(path, values):
    def escape(value):
        return (
            str(value)
            .replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
            .replace(" ", "\\ ")
            .replace("=", "\\=")
            .replace(":", "\\:")
        )

    path.write_text(
        "".join(escape(k) + "=" + escape(v) + "\n" for k, v in values.items()),
        encoding="utf-8",
    )
    return path
