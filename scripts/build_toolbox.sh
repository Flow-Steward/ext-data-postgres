#!/bin/sh
# Build the locked PostgreSQL native driver for both runtime targets.
#
#   scripts/build_toolbox.sh -> bin/<target>/toolbox
#
# The binary carries exactly the source and tool types the variant's prebuilt
# configurations use: they are read from Toolbox's own prebuilt YAML and mapped
# to the packages that register them, so a variant never ships drivers it cannot
# offer and never misses one it needs. Pinned source, bootstrap image, verified SDK and build epoch
# make the result reproducible; scripts/verify_toolbox_artifacts.py checks it.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
test "$#" = 0 || { echo "This build accepts no database selector" >&2; exit 2; }
VARIANT=postgres
TOOLBOX_TAG=v1.17.0
TOOLBOX_COMMIT=45ed205cc8682ed066c82e0b65bcd0e8fc3becce
BOOTSTRAP_IMAGE=golang:1.27.3-bookworm@sha256:dc0963f32053e4bc451d24d87e3892d42d01021b61f093e98eb95c0352bb84b0
PATCH=scripts/patches/0001-flowsteward-driver-bound-query-params.patch
SECURITY_PATCH=scripts/patches/0003-flowsteward-security-dependencies.patch
PROFILE_PATCH=scripts/patches/0004-flowsteward-approved-profiles.patch
VECTOR_PATCH=scripts/patches/0002-flowsteward-managed-vector-safety.patch
BUILD_NAME=${FS_POSTGRES_BUILD_NAME:-fs-postgres-native-build-$$}
GO_CACHE=${FS_POSTGRES_GO_CACHE:-flow-steward-db-batch-go-cache}
BUILD_CACHE=${FS_POSTGRES_BUILD_CACHE:-flow-steward-db-batch-build-cache}
# Fixed connection profiles for this database (MariaDB uses the MySQL protocol).
test -f "$ROOT/scripts/toolbox-prebuilts.txt" \
  || { echo "[FAIL] missing locked profile list" >&2; exit 2; }
PREBUILTS="alloydb-omni alloydb-postgres cloud-sql-postgres postgres"
test "$(tr "\n" " " < "$ROOT/scripts/toolbox-prebuilts.txt" | sed "s/ *$//")" = "$PREBUILTS"
mkdir -p "$ROOT"

# An optional verified local checkout avoids downloading the same pinned source twice.
set --
if test -n "${FS_TOOLBOX_SOURCE_DIR:-}"; then
  set -- --mount "type=bind,source=$FS_TOOLBOX_SOURCE_DIR,target=/upstream,readonly"
fi

docker run --rm --pull=never --name "$BUILD_NAME" --cpus=2 --memory=4g \
  --label flow-steward.owner=ext-data-postgres-native-build \
  -v "$GO_CACHE:/go/pkg/mod" -v "$BUILD_CACHE:/root/.cache/go-build" \
  -e TOOLBOX_TAG="$TOOLBOX_TAG" -e TOOLBOX_COMMIT="$TOOLBOX_COMMIT" -e PATCH="$PATCH" -e VECTOR_PATCH="$VECTOR_PATCH" \
  -e GOMAXPROCS=2 -e PROFILE_PATCH="$PROFILE_PATCH" -e SECURITY_PATCH="$SECURITY_PATCH" -e GOTOOLCHAIN=local -e GOFLAGS=-mod=readonly \
  -e PREBUILTS="$PREBUILTS" -e VARIANT="$VARIANT" \
  -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
  -v "$ROOT:/out" "$@" "$BOOTSTRAP_IMAGE" sh -c '
    set -eu
    sh /out/scripts/install-go-toolchain.sh /out/scripts/go-toolchain-selected.json
    if test -d /upstream/.git; then
      test -z "$(git -c safe.directory=/upstream -C /upstream status --porcelain)"
      cp -a /upstream /src
    else
      git clone --quiet --filter=blob:none --branch "$TOOLBOX_TAG" --depth 1 \
        https://github.com/googleapis/mcp-toolbox.git /src
    fi
    git config --global --add safe.directory /src
    test "$(git -C /src rev-parse HEAD)" = "$TOOLBOX_COMMIT"
    cd /src
    git apply --check "/out/$SECURITY_PATCH"
    git apply "/out/$SECURITY_PATCH"
    sha256sum go.mod go.sum > /tmp/locked-modules.sha256
    git apply --check "/out/$PROFILE_PATCH"
    git apply "/out/$PROFILE_PATCH"
    git apply --check "/out/$PATCH"
    git apply "/out/$PATCH"
    # Observe the native regression before applying the implementation patch.
    git apply --include=internal/sources/postgres/flowsteward_test.go "/out/$VECTOR_PATCH"
    if go test -mod=readonly -p 1 ./internal/sources/postgres -run TestFlowSteward > /tmp/vector-red.log 2>&1; then
      cat /tmp/vector-red.log
      echo "[FAIL] vector safety regression unexpectedly passed before the patch" >&2
      exit 2
    fi
    cat /tmp/vector-red.log
    grep -q "unsafe vector endpoint accepted" /tmp/vector-red.log
    echo "[PASS] managed-vector native regression observed RED"
    git apply --exclude=internal/sources/postgres/flowsteward_test.go "/out/$VECTOR_PATCH"

    # Every resource type the prebuilt configurations name ...
    : > /tmp/types
    for prebuilt in $PREBUILTS; do
      file="internal/prebuiltconfigs/tools/$prebuilt.yaml"
      test -f "$file" || { echo "[FAIL] Toolbox $TOOLBOX_TAG has no prebuilt $prebuilt" >&2; exit 2; }
      sed -n "s/^type:[[:space:]]*\([a-z0-9-]*\).*/\1/p" "$file" >> /tmp/types
    done
    sort -u /tmp/types -o /tmp/types
    # ... mapped to the package that registers it.
    : > /tmp/packages
    while read -r type; do
      dir=$(grep -rlE "^const [A-Za-z]+ string = \"$type\"$" internal/sources internal/tools \
        | xargs -r -n1 dirname | sort -u)
      test "$(printf "%s\n" "$dir" | grep -c .)" = 1 \
        || { echo "[FAIL] type $type is registered by: ${dir:-nothing}" >&2; exit 2; }
      printf "github.com/googleapis/mcp-toolbox/%s\n" "$dir" >> /tmp/packages
    done < /tmp/types
    sort -u /tmp/packages -o /tmp/packages
    awk "NR==FNR { keep[\"\\\"\" \$0 \"\\\"\"] = 1; next }
         /internal\/(sources|tools|prompts|resources)\// { if (keep[\$2]) print; next }
         { print }" /tmp/packages cmd/internal/imports.go > /tmp/imports.go
    test "$(grep -c "internal/\(sources\|tools\|prompts\|resources\)/" /tmp/imports.go)" = "$(wc -l < /tmp/packages)" \
      || { echo "[FAIL] a needed package is not in cmd/internal/imports.go" >&2; exit 2; }
    mv /tmp/imports.go cmd/internal/imports.go
    echo "[PASS] $VARIANT: $(wc -l < /tmp/types) types from $(wc -l < /tmp/packages) packages for: $PREBUILTS"
    cp /tmp/packages "/out/packages.txt"

    go test -mod=readonly -p 1 ./internal/util/flowstewardbinding ./internal/tools/postgres/postgresexecutesql ./internal/sources/postgres
    go test -mod=readonly -p 1 ./internal/sources/postgres -run "TestBuildPostgresURL|TestParseQueryExecMode|TestFlowSteward"
    export SOURCE_DATE_EPOCH=1794536030
    for target in linux-amd64 linux-arm64; do
      arch=${target#linux-}
      mkdir -p "/out/bin/$target"
      CGO_ENABLED=0 GOOS=linux GOARCH="$arch" go build -mod=readonly -p 1 -trimpath -buildvcs=false \
        -ldflags "-s -w -buildid= -X github.com/googleapis/mcp-toolbox/cmd.buildType=flowsteward.dataset-connectors -X github.com/googleapis/mcp-toolbox/cmd.commitSha=$TOOLBOX_COMMIT" \
        -o "/out/bin/$target/toolbox" .
      chmod 0755 "/out/bin/$target/toolbox"
      echo "[PASS] $VARIANT $target: $(stat -c %s "/out/bin/$target/toolbox") bytes"
    done
    echo "[RESOURCE] cgroup memory.peak: $(cat /sys/fs/cgroup/memory.peak) bytes"
    echo "[RESOURCE] cgroup memory.events:"
    cat /sys/fs/cgroup/memory.events
    sha256sum -c /tmp/locked-modules.sha256
    chown -R "$HOST_UID:$HOST_GID" "/out/bin" "/out/packages.txt"
  '

python3 "$ROOT/scripts/provenance.py"
