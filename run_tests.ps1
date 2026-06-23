$script = @"
python test_fixes.py
"@"

$script | Out-File -FilePath "C:\Users\suraj\winkly_bot\temp_test.ps1" -Encoding utf8NoBOM
powershell -File "C:\Users\suraj\winkly_bot\temp_test.ps1"