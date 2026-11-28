#!/usr/bin/env bash
# Dilmaç (iOS): builds ChiakiBridge.xcframework = libchiaki (chiaki-ng) + pc/chiaki_bridge/bridge.c + OpenSSL
# libcrypto + opus, as static libraries for iPhone (arm64), the iOS Simulator (arm64) and the Mac (arm64).
#
# Usage (from anywhere):  ios/scripts/build-chiaki.sh
# Needs: Xcode (license accepted), git, curl, and ios/.venv with cmake, ninja, protobuf, grpcio-tools:
#   python3 -m venv ios/.venv && ios/.venv/bin/pip install cmake ninja protobuf grpcio-tools
# Downloads go to ios/third_party/ and build output to ios/build/ (both git-ignored).
# Re-running skips finished steps; delete ios/build/<step> to redo one.
set -euo pipefail

CHIAKI_TAG="v1.10.1"
OPENSSL_VERSION="3.5.9"
OPENSSL_SHA256="603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a"
OPUS_VERSION="1.5.2"
OPUS_SHA256="65c1d2f78b9f2fb20082c38cbe47c951ad5839345876e46941612ee87f9a7ce1"
IOS_MIN="26.0"
MACOS_MIN="26.0"

IOS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$IOS_DIR/.." && pwd)"
TP="$IOS_DIR/third_party"
DL="$TP/downloads"
SRC="$TP/src"
BUILD="$IOS_DIR/build"
VENV="$IOS_DIR/.venv"
OUT="$BUILD/ChiakiBridge.xcframework"

export PATH="$VENV/bin:$PATH"
for tool in cmake ninja python3 xcrun git curl; do
	command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done
python3 -c "import google.protobuf, grpc_tools" || { echo "ios/.venv needs protobuf and grpcio-tools" >&2; exit 1; }
JOBS="$(sysctl -n hw.ncpu)"

mkdir -p "$DL" "$SRC" "$BUILD"

fetch() { # url sha256 file
	local url="$1" sum="$2" file="$DL/$3"
	[ -f "$file" ] || curl -fsSL "$url" -o "$file"
	echo "$sum  $file" | shasum -a 256 -c - >/dev/null || { echo "checksum mismatch: $file" >&2; rm -f "$file"; exit 1; }
}

# ---------------------------------------------------------------- sources
if [ ! -f "$SRC/chiaki-ng/lib/CMakeLists.txt" ]; then
	rm -rf "$SRC/chiaki-ng"
	git -c advice.detachedHead=false clone --depth 1 --branch "$CHIAKI_TAG" \
		https://github.com/chiaki-ng/chiaki-ng.git "$SRC/chiaki-ng"
	git -C "$SRC/chiaki-ng" submodule update --init --depth 1 \
		third-party/nanopb third-party/jerasure third-party/gf-complete
fi

fetch "https://github.com/openssl/openssl/releases/download/openssl-$OPENSSL_VERSION/openssl-$OPENSSL_VERSION.tar.gz" \
	"$OPENSSL_SHA256" "openssl-$OPENSSL_VERSION.tar.gz"
fetch "https://downloads.xiph.org/releases/opus/opus-$OPUS_VERSION.tar.gz" "$OPUS_SHA256" "opus-$OPUS_VERSION.tar.gz"

# ---------------------------------------------------------------- per slice
# slice: name sdk openssl-target min-version-flag cmake-system deployment-target
build_slice() {
	local name="$1" sdk="$2" ossl_target="$3" min_flag="$4" system="$5" min="$6"
	local prefix="$BUILD/$name/prefix"
	mkdir -p "$prefix"

	# OpenSSL: libcrypto only (chiaki does not use libssl)
	if [ ! -f "$prefix/lib/libcrypto.a" ]; then
		local d="$BUILD/$name/openssl"
		rm -rf "$d" && mkdir -p "$d"
		tar xzf "$DL/openssl-$OPENSSL_VERSION.tar.gz" -C "$d" --strip-components 1
		(cd "$d" && ./Configure "$ossl_target" no-shared no-tests no-apps no-docs no-module no-engine \
			no-ssl3 no-comp no-dso "$min_flag" --prefix="$prefix" --libdir=lib >/dev/null \
			&& make -j"$JOBS" build_libs >/dev/null && make install_dev >/dev/null)
	fi

	local cmake_ios=(-G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_SYSTEM_NAME="$system" -DCMAKE_OSX_SYSROOT="$sdk"
		-DCMAKE_OSX_ARCHITECTURES=arm64 -DCMAKE_OSX_DEPLOYMENT_TARGET="$min"
		-DCMAKE_POLICY_VERSION_MINIMUM=3.5)

	# opus
	if [ ! -f "$prefix/lib/libopus.a" ]; then
		local d="$BUILD/$name/opus"
		rm -rf "$d" && mkdir -p "$d/src"
		tar xzf "$DL/opus-$OPUS_VERSION.tar.gz" -C "$d/src" --strip-components 1
		cmake -S "$d/src" -B "$d/b" "${cmake_ios[@]}" -DCMAKE_INSTALL_PREFIX="$prefix" -DBUILD_SHARED_LIBS=OFF \
			-DOPUS_BUILD_PROGRAMS=OFF -DOPUS_BUILD_TESTING=OFF -DOPUS_INSTALL_PKG_CONFIG_MODULE=OFF \
			-DOPUS_INSTALL_CMAKE_CONFIG_MODULE=OFF >/dev/null
		cmake --build "$d/b" >/dev/null && cmake --install "$d/b" >/dev/null
	fi

	# libchiaki + bridge
	local d="$BUILD/$name/chiaki"
	cmake -S "$IOS_DIR/chiaki" -B "$d" "${cmake_ios[@]}" -DCHIAKI_SOURCE="$SRC/chiaki-ng" \
		-DOPENSSL_PREFIX="$prefix" -DOPUS_PREFIX="$prefix" -DBRIDGE_DIR="$REPO/pc/chiaki_bridge" \
		-DPython3_EXECUTABLE="$VENV/bin/python3" >/dev/null
	cmake --build "$d"

	# one static library per slice
	libtool -static -no_warning_for_no_symbols -o "$BUILD/$name/libChiakiBridge.a" \
		"$d/libdilmac_chiaki.a" "$prefix/lib/libcrypto.a" "$prefix/lib/libopus.a"
}

build_slice ios iphoneos ios64-xcrun "-mios-version-min=$IOS_MIN" iOS "$IOS_MIN"
build_slice sim iphonesimulator iossimulator-arm64-xcrun "-mios-simulator-version-min=$IOS_MIN" iOS "$IOS_MIN"
build_slice macos macosx darwin64-arm64-cc "-mmacosx-version-min=$MACOS_MIN" Darwin "$MACOS_MIN"

# ---------------------------------------------------------------- xcframework
HEADERS="$BUILD/headers"
rm -rf "$HEADERS" && mkdir -p "$HEADERS"
cp "$REPO/pc/chiaki_bridge/bridge.h" "$HEADERS/"
cat > "$HEADERS/module.modulemap" <<'EOF'
module ChiakiBridge {
	header "bridge.h"
	export *
}
EOF

rm -rf "$OUT"
xcodebuild -create-xcframework \
	-library "$BUILD/ios/libChiakiBridge.a" -headers "$HEADERS" \
	-library "$BUILD/sim/libChiakiBridge.a" -headers "$HEADERS" \
	-library "$BUILD/macos/libChiakiBridge.a" -headers "$HEADERS" \
	-output "$OUT" >/dev/null
echo "built: $OUT"
