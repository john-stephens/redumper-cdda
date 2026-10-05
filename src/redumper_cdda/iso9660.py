"""Raw CD data-sector conversion and ISO9660 validation."""


SECTOR_SIZE = 2352
ISO_SECTOR_SIZE = 2048


def _both_endian_32(value):
    return value.to_bytes(4, "little") + value.to_bytes(4, "big")


def _relative_lba(absolute_lba, session_start_lba, volume_sectors):
    relative = absolute_lba - session_start_lba
    if relative < 0 or relative >= volume_sectors:
        raise RuntimeError("ISO9660 extent lies outside the rebased volume.")
    return relative


def _rebase_path_table(image, location, size, byteorder, session_start_lba, volume_sectors):
    image.seek(location * ISO_SECTOR_SIZE)
    table = bytearray(image.read(size))
    if len(table) != size:
        raise RuntimeError("Short ISO9660 path table.")
    offset = 0
    while offset < size:
        identifier_length = table[offset]
        if identifier_length == 0:
            if any(table[offset:]):
                raise RuntimeError("Malformed ISO9660 path table.")
            break
        entry_length = 8 + identifier_length + (identifier_length & 1)
        if offset + entry_length > size:
            raise RuntimeError("Malformed ISO9660 path table.")
        absolute_lba = int.from_bytes(table[offset + 2:offset + 6], byteorder)
        relative_lba = _relative_lba(
            absolute_lba, session_start_lba, volume_sectors
        )
        table[offset + 2:offset + 6] = relative_lba.to_bytes(4, byteorder)
        offset += entry_length
    image.seek(location * ISO_SECTOR_SIZE)
    image.write(table)


def _rebase_directories(image, roots, session_start_lba, volume_sectors):
    pending = list(roots)
    visited = set()
    while pending:
        absolute_lba, byte_count = pending.pop()
        if absolute_lba in visited:
            continue
        visited.add(absolute_lba)
        relative_lba = _relative_lba(
            absolute_lba, session_start_lba, volume_sectors
        )
        image.seek(relative_lba * ISO_SECTOR_SIZE)
        directory = bytearray(image.read(byte_count))
        if len(directory) != byte_count:
            raise RuntimeError("Short ISO9660 directory extent.")
        offset = 0
        while offset < byte_count:
            record_length = directory[offset]
            if record_length == 0:
                offset += ISO_SECTOR_SIZE - (offset % ISO_SECTOR_SIZE)
                continue
            if (
                record_length < 34
                or offset % ISO_SECTOR_SIZE + record_length > ISO_SECTOR_SIZE
                or offset + record_length > byte_count
            ):
                raise RuntimeError("Malformed ISO9660 directory record.")
            absolute_extent = int.from_bytes(
                directory[offset + 2:offset + 6], "little"
            )
            relative_extent = _relative_lba(
                absolute_extent, session_start_lba, volume_sectors
            )
            directory[offset + 2:offset + 10] = _both_endian_32(relative_extent)
            identifier_length = directory[offset + 32]
            identifier = directory[
                offset + 33:offset + 33 + identifier_length
            ]
            if directory[offset + 25] & 0x02 and identifier not in (b"\0", b"\1"):
                child_size = int.from_bytes(
                    directory[offset + 10:offset + 14], "little"
                )
                pending.append((absolute_extent, child_size))
            offset += record_length
        image.seek(relative_lba * ISO_SECTOR_SIZE)
        image.write(directory)


def rebase_iso9660_session(
    iso_path, session_start_lba, total_sectors, expected_track_sectors=None
):
    """Convert multisession absolute ISO9660 LBAs to track-relative LBAs."""

    if session_start_lba == 0:
        return
    roots = []
    patched_tables = set()
    volume_sectors = None
    expected_track_sectors = expected_track_sectors or total_sectors
    with iso_path.open("r+b") as image:
        for sector_number in range(16, min(total_sectors, 256)):
            image.seek(sector_number * ISO_SECTOR_SIZE)
            descriptor = bytearray(image.read(ISO_SECTOR_SIZE))
            if len(descriptor) != ISO_SECTOR_SIZE:
                raise RuntimeError("Short ISO9660 volume descriptor.")
            if descriptor[1:6] != b"CD001" or descriptor[6] != 1:
                raise RuntimeError(
                    "Converted data track has an invalid ISO9660 volume descriptor."
                )
            descriptor_type = descriptor[0]
            if descriptor_type == 255:
                break
            if descriptor_type not in (1, 2):
                continue
            absolute_volume_sectors = int.from_bytes(descriptor[80:84], "little")
            if absolute_volume_sectors != int.from_bytes(descriptor[84:88], "big"):
                raise RuntimeError("ISO9660 volume-space size is invalid.")
            if absolute_volume_sectors <= expected_track_sectors:
                return
            volume_sectors = absolute_volume_sectors - session_start_lba
            if (
                volume_sectors <= 0
                or volume_sectors > total_sectors
                or volume_sectors > expected_track_sectors
            ):
                raise RuntimeError("ISO9660 rebased volume-space size is invalid.")
            descriptor[80:88] = _both_endian_32(volume_sectors)
            path_table_size = int.from_bytes(descriptor[132:136], "little")
            if path_table_size != int.from_bytes(descriptor[136:140], "big"):
                raise RuntimeError("ISO9660 path-table size is invalid.")
            for field, byteorder in ((140, "little"), (144, "little"), (148, "big"), (152, "big")):
                absolute_location = int.from_bytes(
                    descriptor[field:field + 4], byteorder
                )
                if absolute_location == 0:
                    continue
                relative_location = _relative_lba(
                    absolute_location, session_start_lba, volume_sectors
                )
                descriptor[field:field + 4] = relative_location.to_bytes(
                    4, byteorder
                )
                table = (relative_location, path_table_size, byteorder)
                if table not in patched_tables:
                    _rebase_path_table(
                        image,
                        relative_location,
                        path_table_size,
                        byteorder,
                        session_start_lba,
                        volume_sectors,
                    )
                    patched_tables.add(table)
            root = descriptor[156:190]
            absolute_root = int.from_bytes(root[2:6], "little")
            if absolute_root != int.from_bytes(root[6:10], "big"):
                raise RuntimeError("ISO9660 root directory extent is invalid.")
            relative_root = _relative_lba(
                absolute_root, session_start_lba, volume_sectors
            )
            descriptor[158:166] = _both_endian_32(relative_root)
            root_size = int.from_bytes(root[10:14], "little")
            roots.append((absolute_root, root_size))
            image.seek(sector_number * ISO_SECTOR_SIZE)
            image.write(descriptor)
        if volume_sectors is None:
            raise RuntimeError(
                "Converted data track has no supported ISO9660 volume descriptor."
            )
        _rebase_directories(
            image, roots, session_start_lba, volume_sectors
        )


def extract_iso_payload(raw_sector, track_type):
    if track_type == "MODE1/2048":
        if len(raw_sector) != ISO_SECTOR_SIZE:
            raise RuntimeError("Short MODE1/2048 sector.")
        return raw_sector

    if len(raw_sector) != SECTOR_SIZE:
        raise RuntimeError(f"Short {track_type} sector.")

    expected_sync = b"\x00" + b"\xff" * 10 + b"\x00"

    if raw_sector[:12] != expected_sync:
        raise RuntimeError(f"Invalid sync pattern in {track_type} sector.")

    if track_type == "MODE1/2352":
        if raw_sector[15] != 1:
            raise RuntimeError("MODE1/2352 sector has the wrong mode byte.")
        return raw_sector[16:16 + ISO_SECTOR_SIZE]

    if track_type == "MODE2/2352":
        if raw_sector[15] != 2:
            raise RuntimeError("MODE2/2352 sector has the wrong mode byte.")

        if raw_sector[16:20] != raw_sector[20:24]:
            raise RuntimeError("MODE2/2352 sector has mismatched subheaders.")

        # A 2048-byte ISO image is the logical-block view of the data track.
        # Form 2 sectors carry 2324 user-data bytes, but enhanced CDs can mix
        # them into an otherwise ISO9660 track.  Retain the first logical
        # block, as established CD image converters do; the staged redumper
        # dump remains the lossless source for all 2324 bytes.
        return raw_sector[24:24 + ISO_SECTOR_SIZE]

    raise RuntimeError(f"Unsupported data-track mode {track_type}.")


def read_iso9660_volume_size(iso_path, total_sectors):
    if total_sectors <= 16:
        raise RuntimeError("Converted data track is too short for ISO9660.")

    with iso_path.open("rb") as iso_file:
        primary = None

        for sector_number in range(16, min(total_sectors, 256)):
            iso_file.seek(sector_number * ISO_SECTOR_SIZE)
            descriptor = iso_file.read(ISO_SECTOR_SIZE)

            if len(descriptor) != ISO_SECTOR_SIZE:
                raise RuntimeError("Short ISO9660 volume descriptor.")

            if descriptor[1:6] != b"CD001" or descriptor[6] != 1:
                raise RuntimeError(
                    "Converted data track has an invalid ISO9660 "
                    "volume descriptor."
                )

            descriptor_type = descriptor[0]

            if descriptor_type == 1:
                primary = descriptor
                break

            if descriptor_type == 255:
                break

    if primary is None:
        raise RuntimeError(
            "Converted data track has no ISO9660 primary volume descriptor."
        )

    volume_sectors_le = int.from_bytes(primary[80:84], byteorder="little")
    volume_sectors_be = int.from_bytes(primary[84:88], byteorder="big")
    block_size_le = int.from_bytes(primary[128:130], byteorder="little")
    block_size_be = int.from_bytes(primary[130:132], byteorder="big")

    if volume_sectors_le == 0 or volume_sectors_le != volume_sectors_be:
        raise RuntimeError("ISO9660 volume-space size is invalid.")

    if block_size_le != ISO_SECTOR_SIZE or block_size_be != ISO_SECTOR_SIZE:
        raise RuntimeError("ISO9660 logical block size is not 2048 bytes.")

    if volume_sectors_le > total_sectors:
        raise RuntimeError(
            "ISO9660 volume requires more sectors than the split data track provides."
        )

    root_record = primary[156:190]

    if root_record[0] < 34 or not (root_record[25] & 0x02):
        raise RuntimeError("ISO9660 root directory record is invalid.")

    root_extent_le = int.from_bytes(root_record[2:6], byteorder="little")
    root_extent_be = int.from_bytes(root_record[6:10], byteorder="big")
    root_size_le = int.from_bytes(root_record[10:14], byteorder="little")
    root_size_be = int.from_bytes(root_record[14:18], byteorder="big")

    if (
        root_extent_le != root_extent_be
        or root_size_le == 0
        or root_size_le != root_size_be
    ):
        raise RuntimeError("ISO9660 root directory extent is invalid.")

    root_sectors = (root_size_le + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE

    if root_extent_le + root_sectors > volume_sectors_le:
        raise RuntimeError("ISO9660 root directory lies outside the volume.")

    return volume_sectors_le


def data_track_to_iso(data_track, iso_path, verbose=False, output=print):
    iso_path.parent.mkdir(parents=True, exist_ok=True)
    track_type = data_track["track_type"]
    raw_sector_size = data_track["sector_size"]
    track_sectors = data_track.get("track_sectors", data_track["sectors"])
    available_sectors = data_track["sectors"]
    converted_sectors = 0

    with (
        data_track["path"].open("rb") as source,
        iso_path.open("wb") as destination,
    ):
        source.seek(data_track["start_sector"] * raw_sector_size)

        for sector_index in range(available_sectors):
            raw_sector = source.read(raw_sector_size)

            if len(raw_sector) != raw_sector_size:
                raise RuntimeError(
                    "Unexpected end of data BIN at sector "
                    f"{sector_index:,}."
                )

            destination.write(extract_iso_payload(raw_sector, track_type))
            converted_sectors += 1

    rebase_iso9660_session(
        iso_path,
        data_track.get("track_begin_lba", 0),
        converted_sectors,
        track_sectors,
    )
    volume_sectors = read_iso9660_volume_size(iso_path, converted_sectors)

    with iso_path.open("r+b") as destination:
        destination.truncate(volume_sectors * ISO_SECTOR_SIZE)

    if verbose:
        output()
        output("ISO conversion")
        output("--------------")
        output(f"Track:               {data_track['track']:02d}")
        output(f"Mode:                {track_type}")
        output(f"BIN:                 {data_track['path']}")
        output(
            f"INDEX 01 offset:     "
            f"{data_track['start_sector']:,} sectors"
        )
        output(f"ISO9660 sectors:     {volume_sectors:,}")
        output(f"Temporary ISO:       {iso_path}")
