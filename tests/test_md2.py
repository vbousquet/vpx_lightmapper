"""MD2 checks against the test suite of RFC 1319, appendix A.5.

Run with: python3 tests/test_md2.py
"""

import sys

from _addon import load

vlm_md2 = load('vlm_md2')

RFC1319_VECTORS = (
    ('', '8350e5a3e24c153df2275c9f80692773'),
    ('a', '32ec01ec4a6dac72c0ab96fb34c0b5d1'),
    ('abc', 'da853b0d3f88d99b30283a69e6ded6bb'),
    ('message digest', 'ab4f496bfb2a530b219ff33031fe06b0'),
    ('abcdefghijklmnopqrstuvwxyz', '4e8ddff3650292ab5a4108c3aa47940b'),
    ('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789',
     'da33def2a42df13975352846c30338cd'),
    ('1234567890' * 8, 'd5976f79d83d3a0dc9806c3c66f3efd8'),
)


def test_rfc1319_vectors():
    for text, expected in RFC1319_VECTORS:
        assert vlm_md2.new(text.encode()).hexdigest() == expected, text


def test_incremental_update_matches_single_shot():
    # The exporter hashes one stream at a time, so chunking must not matter.
    data = bytes(range(256)) * 7
    reference = vlm_md2.new(data).digest()
    for chunk in (1, 15, 16, 17, 64, 1000):
        hasher = vlm_md2.new()
        for offset in range(0, len(data), chunk):
            hasher.update(data[offset:offset + chunk])
        assert hasher.digest() == reference, chunk


def test_digest_does_not_finalise_the_object():
    hasher = vlm_md2.new(b'ab')
    assert hasher.digest() == vlm_md2.new(b'ab').digest()
    hasher.update(b'c')
    assert hasher.hexdigest() == 'da853b0d3f88d99b30283a69e6ded6bb'


def main():
    failures = 0
    for name, test in sorted(globals().items()):
        if not name.startswith('test_'):
            continue
        try:
            test()
        except AssertionError as error:
            failures += 1
            print(f'FAIL {name}: {error}')
        else:
            print(f'ok   {name}')
    print('MD2: all tests passed' if not failures else f'MD2: {failures} failure(s)')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
