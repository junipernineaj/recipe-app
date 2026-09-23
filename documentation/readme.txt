Handy scripts:

find ./Cookbooks -type f -iname '*.pdf' -exec basename {} \; | sort > /tmp/local_names.txt

ssh aj9@192.168.1.40 "find /media/aj9/Juniper13/cookbook_ocr_output -type f -exec basename {} \;" | sort > /tmp/remote_names.txt

echo "=== Local files NOT found in cookbook_ocr_output (genuinely new) ==="
comm -23 /tmp/local_names.txt /tmp/remote_names.txt

echo ""
echo "=== Local files that already match a name in cookbook_ocr_output ==="
comm -12 /tmp/local_names.txt /tmp/remote_names.txt


