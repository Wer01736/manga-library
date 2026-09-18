$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms

$owner = New-Object System.Windows.Forms.Form
$owner.Text = 'Select comic folder'
$owner.StartPosition = 'CenterScreen'
$owner.ShowInTaskbar = $false
$owner.TopMost = $true
$owner.Width = 1
$owner.Height = 1
$owner.Opacity = 0
$owner.Show()

try {
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = 'Select a folder to add to the comic library'
    $dialog.Filter = 'All files (*.*)|*.*'
    $dialog.CheckFileExists = $false
    $dialog.CheckPathExists = $true
    $dialog.ValidateNames = $false
    $dialog.FileName = 'Select this folder'
    $result = $dialog.ShowDialog($owner)
    if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
        $selected = [System.IO.Path]::GetDirectoryName($dialog.FileName)
        [Console]::Out.Write($selected)
    }
}
finally {
    $owner.Close()
    $owner.Dispose()
}
