
yml=*.yml
files=$1$yml

echo "$files"

for f in $files; do
    echo $f
    CUDA_VISIBLE_DEVICES=$2 python -W ignore validate.py --config $f
done