param([string]$ProgressFile,[int]$Owner)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form=New-Object Windows.Forms.Form
$form.Text='AutoReview 正在启动';$form.Size=New-Object Drawing.Size(440,180);$form.StartPosition='CenterScreen';$form.FormBorderStyle='FixedDialog';$form.MaximizeBox=$false;$form.MinimizeBox=$false;$form.ControlBox=$false
$label=New-Object Windows.Forms.Label;$label.Location=New-Object Drawing.Point(24,25);$label.Size=New-Object Drawing.Size(380,44);$label.Text='正在检查当前源码…';$form.Controls.Add($label)
$bar=New-Object Windows.Forms.ProgressBar;$bar.Location=New-Object Drawing.Point(24,80);$bar.Size=New-Object Drawing.Size(375,20);$bar.Value=5;$form.Controls.Add($bar)
$timer=New-Object Windows.Forms.Timer;$timer.Interval=250
$timer.Add_Tick({try {if(-not(Get-Process -Id $Owner -ErrorAction SilentlyContinue)){$form.Close();return};if(Test-Path -LiteralPath $ProgressFile){$v=Get-Content -LiteralPath $ProgressFile -Raw|ConvertFrom-Json;$label.Text=$v.status;$bar.Value=[Math]::Min(100,[Math]::Max(0,[int]$v.percent));if($v.done){$form.Close()}}}catch{}})
$timer.Start();[void]$form.ShowDialog();$timer.Stop();$form.Dispose()
