BUILD_DIR="build/sphinx"
[[ -d $BUILD_DIR ]] && rm -rf $BUILD_DIR
[[ ! -d venv ]] && python3 -m venv venv
source venv/bin/activate
python3 -m pip install -e .
[[ ! -f VOYAGE_API_KEY ]] && echo "ERROR: VOYAGE_API_KEY file not found" && return 1
export VOYAGE_API_KEY=$(cat VOYAGE_API_KEY)
sphinx-build -M html ./doc $BUILD_DIR
deactivate
