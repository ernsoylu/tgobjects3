"""Unit and property tests for :mod:`gateway.app.s3.xml_codecs`.

Covers the S3 XML serialisers for bucket operations (requirement 4.1), object
listing (requirements 5.1, 5.2, 5.3), multipart upload (requirements 10.1, 10.7,
10.8, 10.9), the error document, and the DeleteObjects / CompleteMultipartUpload
request parsers (requirements 9.6, 10.4). Every serialiser's output is reparsed
with the standard library to assert both well-formedness and that the S3
namespace is applied to the document root.
"""

from __future__ import annotations

from datetime import UTC, datetime
from xml.etree import ElementTree as ET

import pytest
from gateway.app.s3.xml_codecs import (
    S3_NAMESPACE,
    Bucket,
    CommonPrefix,
    CompleteMultipartUploadResponse,
    DeletedObject,
    DeleteError,
    DeleteObjectsResponse,
    Error,
    GetBucketLocationResponse,
    InitiateMultipartUploadResponse,
    ListBucketsResponse,
    ListMultipartUploadsResponse,
    ListObjectsResponse,
    ListObjectsV2Response,
    ListPartsResponse,
    MalformedXMLError,
    MultipartUpload,
    ObjectEntry,
    Part,
    deserialise_delete_objects_request,
    parse_complete_multipart_upload_request,
    serialise_complete_multipart_upload,
    serialise_delete_objects_response,
    serialise_error,
    serialise_get_bucket_location,
    serialise_initiate_multipart_upload,
    serialise_list_buckets,
    serialise_list_multipart_uploads,
    serialise_list_objects,
    serialise_list_objects_v2,
    serialise_list_parts,
)
from hypothesis import given
from hypothesis import strategies as st

_TS = datetime(2024, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)


def _parse(data: bytes) -> ET.Element:
    """Reparse serialiser output, asserting the S3 namespace on the root."""
    root = ET.fromstring(data)
    assert root.tag.startswith(f"{{{S3_NAMESPACE}}}")
    return root


def _q(local: str) -> str:
    return f"{{{S3_NAMESPACE}}}{local}"


# ---------------------------------------------------------------------------
# Bucket operations (requirement 4)
# ---------------------------------------------------------------------------


def test_list_buckets_lists_every_bucket() -> None:
    resp = ListBucketsResponse(
        buckets=(Bucket("alpha", _TS), Bucket("beta", _TS)),
        owner_id="owner-1",
        owner_display_name="Owner One",
    )
    root = _parse(serialise_list_buckets(resp))
    names = [b.find(_q("Name")).text for b in root.find(_q("Buckets"))]  # type: ignore[union-attr]
    assert names == ["alpha", "beta"]
    assert root.find(_q("Owner")).find(_q("ID")).text == "owner-1"  # type: ignore[union-attr]


def test_get_bucket_location_carries_region() -> None:
    root = _parse(serialise_get_bucket_location(GetBucketLocationResponse("eu-west-1")))
    assert root.text == "eu-west-1"


def test_get_bucket_location_allows_empty_us_east_1_form() -> None:
    root = _parse(serialise_get_bucket_location(GetBucketLocationResponse("")))
    assert (root.text or "") == ""


# ---------------------------------------------------------------------------
# Object listing (requirement 5)
# ---------------------------------------------------------------------------


def test_list_objects_v2_contents_and_common_prefixes() -> None:
    resp = ListObjectsV2Response(
        name="bucket",
        prefix="p/",
        key_count=1,
        is_truncated=True,
        delimiter="/",
        next_continuation_token="tok-2",
        contents=(ObjectEntry("p/a", _TS, '"etag"', 10),),
        common_prefixes=(CommonPrefix("p/sub/"),),
    )
    root = _parse(serialise_list_objects_v2(resp))
    assert root.find(_q("IsTruncated")).text == "true"  # type: ignore[union-attr]
    assert root.find(_q("NextContinuationToken")).text == "tok-2"  # type: ignore[union-attr]
    assert root.find(_q("Contents")).find(_q("Key")).text == "p/a"  # type: ignore[union-attr]
    assert root.find(_q("Contents")).find(_q("Size")).text == "10"  # type: ignore[union-attr]
    assert root.find(_q("CommonPrefixes")).find(_q("Prefix")).text == "p/sub/"  # type: ignore[union-attr]


def test_list_objects_v2_omits_optional_delimiter_when_absent() -> None:
    root = _parse(serialise_list_objects_v2(ListObjectsV2Response(name="bucket")))
    assert root.find(_q("Delimiter")) is None
    assert root.find(_q("NextContinuationToken")) is None


def test_list_objects_v1_uses_marker_pagination() -> None:
    resp = ListObjectsResponse(
        name="bucket",
        marker="m0",
        is_truncated=True,
        next_marker="m1",
        contents=(ObjectEntry("a", _TS, '"e"', 3),),
    )
    root = _parse(serialise_list_objects(resp))
    assert root.find(_q("Marker")).text == "m0"  # type: ignore[union-attr]
    assert root.find(_q("NextMarker")).text == "m1"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Multipart upload (requirement 10)
# ---------------------------------------------------------------------------


def test_initiate_multipart_upload() -> None:
    root = _parse(
        serialise_initiate_multipart_upload(
            InitiateMultipartUploadResponse("bucket", "key", "upl-1")
        )
    )
    assert root.find(_q("UploadId")).text == "upl-1"  # type: ignore[union-attr]


def test_complete_multipart_upload_composite_etag() -> None:
    root = _parse(
        serialise_complete_multipart_upload(
            CompleteMultipartUploadResponse(
                location="http://host/bucket/key",
                bucket="bucket",
                key="key",
                etag='"abc-3"',
            )
        )
    )
    assert root.find(_q("ETag")).text == '"abc-3"'  # type: ignore[union-attr]


def test_list_parts() -> None:
    resp = ListPartsResponse(
        bucket="bucket",
        key="key",
        upload_id="upl-1",
        parts=(Part(1, _TS, '"e1"', 5), Part(2, _TS, '"e2"', 6)),
    )
    root = _parse(serialise_list_parts(resp))
    numbers = [p.find(_q("PartNumber")).text for p in root.findall(_q("Part"))]  # type: ignore[union-attr]
    assert numbers == ["1", "2"]


def test_list_multipart_uploads() -> None:
    resp = ListMultipartUploadsResponse(
        bucket="bucket",
        uploads=(MultipartUpload("key", "upl-1", _TS),),
    )
    root = _parse(serialise_list_multipart_uploads(resp))
    assert root.find(_q("Upload")).find(_q("UploadId")).text == "upl-1"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Batch delete (requirement 9.6)
# ---------------------------------------------------------------------------


def test_delete_objects_request_round_trip() -> None:
    xml = b"""<?xml version="1.0"?>
    <Delete xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <Quiet>true</Quiet>
      <Object><Key>a</Key></Object>
      <Object><Key>b</Key><VersionId>v2</VersionId></Object>
    </Delete>"""
    req = deserialise_delete_objects_request(xml)
    assert req.quiet is True
    assert [o.key for o in req.objects] == ["a", "b"]
    assert req.objects[1].version_id == "v2"


def test_delete_objects_request_without_namespace() -> None:
    xml = b"<Delete><Object><Key>only</Key></Object></Delete>"
    req = deserialise_delete_objects_request(xml)
    assert [o.key for o in req.objects] == ["only"]
    assert req.quiet is False


def test_delete_objects_request_missing_key_rejected() -> None:
    with pytest.raises(MalformedXMLError):
        deserialise_delete_objects_request(b"<Delete><Object></Object></Delete>")


def test_delete_objects_request_wrong_root_rejected() -> None:
    with pytest.raises(MalformedXMLError):
        deserialise_delete_objects_request(b"<NotDelete/>")


def test_delete_objects_request_malformed_rejected() -> None:
    with pytest.raises(MalformedXMLError):
        deserialise_delete_objects_request(b"<Delete>")


def test_delete_objects_response_reports_both_outcomes() -> None:
    resp = DeleteObjectsResponse(
        deleted=(DeletedObject("ok"),),
        errors=(DeleteError("bad", "AccessDenied", "nope"),),
    )
    root = _parse(serialise_delete_objects_response(resp))
    assert root.find(_q("Deleted")).find(_q("Key")).text == "ok"  # type: ignore[union-attr]
    err = root.find(_q("Error"))
    assert err.find(_q("Code")).text == "AccessDenied"  # type: ignore[union-attr]
    assert err.find(_q("Message")).text == "nope"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# CompleteMultipartUpload request parsing (requirements 10.4, 10.5)
# ---------------------------------------------------------------------------


def test_parse_complete_multipart_upload_request() -> None:
    xml = b"""<CompleteMultipartUpload>
      <Part><PartNumber>1</PartNumber><ETag>"e1"</ETag></Part>
      <Part><PartNumber>2</PartNumber><ETag>"e2"</ETag></Part>
    </CompleteMultipartUpload>"""
    parts = parse_complete_multipart_upload_request(xml)
    assert [(p.part_number, p.etag) for p in parts] == [(1, '"e1"'), (2, '"e2"')]


def test_parse_complete_multipart_upload_bad_part_number_rejected() -> None:
    xml = (
        b"<CompleteMultipartUpload><Part><PartNumber>x</PartNumber>"
        b"<ETag>e</ETag></Part></CompleteMultipartUpload>"
    )
    with pytest.raises(MalformedXMLError):
        parse_complete_multipart_upload_request(xml)


# ---------------------------------------------------------------------------
# Error document
# ---------------------------------------------------------------------------


def test_error_document_includes_extra_elements() -> None:
    err = Error(
        code="NoSuchBucket",
        message="The specified bucket does not exist",
        resource="/bucket",
        request_id="req-1",
        extra={"BucketName": "bucket"},
    )
    root = _parse(serialise_error(err))
    assert root.find(_q("Code")).text == "NoSuchBucket"  # type: ignore[union-attr]
    assert root.find(_q("BucketName")).text == "bucket"  # type: ignore[union-attr]
    assert root.find(_q("Resource")).text == "/bucket"  # type: ignore[union-attr]


def test_error_special_characters_are_escaped() -> None:
    # A message containing XML metacharacters must round-trip through parsing,
    # proving the serialiser escapes rather than string-formats.
    err = Error(code="InvalidArgument", message='bad <value> & "quote"')
    root = _parse(serialise_error(err))
    assert root.find(_q("Message")).text == 'bad <value> & "quote"'  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Property test: listing output is always well-formed and preserves order
# ---------------------------------------------------------------------------

# Restrict to characters that are both legal in XML 1.0 text and stable across a
# parse round-trip. XML 1.0 forbids most C0 control characters outright, and
# normalises carriage returns to line feeds on parse, so neither belongs in a
# round-trip generator — those are properties of XML itself, not the codec, and
# the S3 layer handles such keys via EncodingType at a higher layer.
_key_text = st.text(
    alphabet=st.characters(
        min_codepoint=0x20,
        blacklist_categories=("Cs",),
    ),
    min_size=1,
    max_size=20,
)


@given(
    keys=st.lists(_key_text, max_size=8),
    prefixes=st.lists(_key_text, max_size=8),
)
def test_list_objects_v2_preserves_order_and_is_well_formed(
    keys: list[str], prefixes: list[str]
) -> None:
    """ListObjectsV2 output reparses and preserves Contents/CommonPrefixes order.

    Validates: Requirements 5.2
    """
    resp = ListObjectsV2Response(
        name="bucket",
        key_count=len(keys),
        contents=tuple(ObjectEntry(k, _TS, '"e"', 1) for k in keys),
        common_prefixes=tuple(CommonPrefix(p) for p in prefixes),
    )
    root = _parse(serialise_list_objects_v2(resp))
    out_keys = [c.find(_q("Key")).text for c in root.findall(_q("Contents"))]  # type: ignore[union-attr]
    out_prefixes = [
        c.find(_q("Prefix")).text  # type: ignore[union-attr]
        for c in root.findall(_q("CommonPrefixes"))
    ]
    assert out_keys == keys
    assert out_prefixes == prefixes
