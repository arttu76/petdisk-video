#!/bin/sh
# Build the PET client: a tokenized BASIC loop (stub.bas.in) with the
# machine code (client.a) at $0800 in one PRG.
#   serve/PDV80.PRG     80 columns, device 10 (the network drive)
#   serve/PDV40.PRG     40 columns, device 10
#   build/PDV8.PRG      80 columns, device 8 (emulator tests)
set -e
cd "$(dirname "$0")"
mkdir -p serve build

one() {  # one <device> <columns> <program name> <video file> <output> <results file>
    sed -e "s/@DEV@/$1/" -e "s/@COLS@/$2/" -e "s/@NAME@/$3/" -e "s/@VIDEO@/$4/" -e "s/@RESULTS@/$6/" -e "/^#/d" stub.bas.in > build/stub.bas
    petcat -w2 -l 0401 -o build/stub.prg -- build/stub.bas
    acme -DDEV=$1 -DCOLS=$2 -f plain -o build/ml.bin client.a
    python3 - "$5" <<'PY'
import sys
stub = open("build/stub.prg", "rb").read()
ml = open("build/ml.bin", "rb").read()
end = 0x0401 + len(stub) - 2
if end > 0x07fe:                # $07FE-$07FF: SRVSTOP and KEYOUT of client.a
    sys.exit("BASIC part too long: ends at $%04X" % end)
data = stub + bytes(0x0800 - end) + ml
open(sys.argv[1], "wb").write(data)
PY
}

one 10 80 pdv80 video80 serve/PDV80.PRG results80
one 10 40 pdv40 video40 serve/PDV40.PRG results40
one 8 80 pdv video80 build/PDV8.PRG results80
ls -l serve/*.PRG build/PDV8.PRG
