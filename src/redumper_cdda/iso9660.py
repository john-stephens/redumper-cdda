"""Raw CD data-sector conversion and ISO9660 validation."""


SECTOR_SIZE = 2352
ISO_SECTOR_SIZE = 2048


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

        if raw_sector[18] & 0x20:
            raise RuntimeError(
                "MODE2 Form 2 sectors cannot be represented in "
                "a 2048-byte-sector ISO."
            )

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


def data_track_to_iso(data_track, iso_path, verbose=False):
    iso_path.parent.mkdir(parents=True, exist_ok=True)
    track_type = data_track["track_type"]
    raw_sector_size = data_track["sector_size"]
    converted_sectors = 0

    with (
        data_track["path"].open("rb") as source,
        iso_path.open("wb") as destination,
    ):
        source.seek(data_track["start_sector"] * raw_sector_size)

        for sector_index in range(data_track["sectors"]):
            raw_sector = source.read(raw_sector_size)

            if len(raw_sector) != raw_sector_size:
                raise RuntimeError(
                    "Unexpected end of data BIN at sector "
                    f"{sector_index:,}."
                )

            destination.write(extract_iso_payload(raw_sector, track_type))
            converted_sectors += 1

    volume_sectors = read_iso9660_volume_size(iso_path, converted_sectors)

    with iso_path.open("r+b") as destination:
        destination.truncate(volume_sectors * ISO_SECTOR_SIZE)

    if verbose:
        print()
        print("ISO conversion")
        print("--------------")
        print(f"Track:               {data_track['track']:02d}")
        print(f"Mode:                {track_type}")
        print(f"BIN:                 {data_track['path']}")
        print(
            f"INDEX 01 offset:     "
            f"{data_track['start_sector']:,} sectors"
        )
        print(f"ISO9660 sectors:     {volume_sectors:,}")
        print(f"Temporary ISO:       {iso_path}")
