# Hub label printer - background agent for a Windows laptop (served by the label station, web/src/labelstation.js).
# Installed by install-label-printer.bat into %LOCALAPPDATA%\Hub-Label-Printer, started hidden and at sign-in.
# Long-polls the station's print queue, prints each label picture on the label printer at 4x6, keeps the laptop
# from sleeping, and logs to agent.log next to itself. The station fills in its address and the version when it serves it.
$ErrorActionPreference = 'Continue'
$Base = '__BASE__'
$Version = '__VER__'
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Log = Join-Path $Dir 'agent.log'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public static class HubAwake { [DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint f); }
"@
# Printing past the Windows driver: the picture as the printer's own commands, written to the spooler as RAW.
# (The driver took minutes to open each job on these printers; the drawing itself takes a fraction of a second.)
$script:RawErr = ''
try {
  Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @"
using System; using System.Drawing; using System.Drawing.Imaging; using System.Runtime.InteropServices; using System.Text;
public static class HubRaw {
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
  public class DocInfo {
    [MarshalAs(UnmanagedType.LPWStr)] public string Name;
    [MarshalAs(UnmanagedType.LPWStr)] public string OutputFile;
    [MarshalAs(UnmanagedType.LPWStr)] public string DataType;
  }
  [DllImport("winspool.drv", EntryPoint = "OpenPrinterW", SetLastError = true, CharSet = CharSet.Unicode, ExactSpelling = true)]
  static extern bool OpenPrinter(string name, out IntPtr handle, IntPtr defaults);
  [DllImport("winspool.drv", SetLastError = true, ExactSpelling = true)]
  static extern bool ClosePrinter(IntPtr handle);
  [DllImport("winspool.drv", EntryPoint = "StartDocPrinterW", SetLastError = true, CharSet = CharSet.Unicode, ExactSpelling = true)]
  static extern int StartDocPrinter(IntPtr handle, int level, [In, MarshalAs(UnmanagedType.LPStruct)] DocInfo info);
  [DllImport("winspool.drv", SetLastError = true, ExactSpelling = true)]
  static extern bool EndDocPrinter(IntPtr handle);
  [DllImport("winspool.drv", SetLastError = true, ExactSpelling = true)]
  static extern bool StartPagePrinter(IntPtr handle);
  [DllImport("winspool.drv", SetLastError = true, ExactSpelling = true)]
  static extern bool EndPagePrinter(IntPtr handle);
  [DllImport("winspool.drv", SetLastError = true, ExactSpelling = true)]
  static extern bool WritePrinter(IntPtr handle, byte[] data, int count, out int written);

  public static void Send(string printer, byte[] data) {
    IntPtr h;
    if (!OpenPrinter(printer, out h, IntPtr.Zero)) throw new Exception("OpenPrinter failed, error " + Marshal.GetLastWin32Error());
    try {
      DocInfo di = new DocInfo();
      di.Name = "Hub label";
      di.DataType = "RAW";
      if (StartDocPrinter(h, 1, di) == 0) throw new Exception("StartDocPrinter failed, error " + Marshal.GetLastWin32Error());
      try {
        if (!StartPagePrinter(h)) throw new Exception("StartPagePrinter failed, error " + Marshal.GetLastWin32Error());
        int written;
        bool ok = WritePrinter(h, data, data.Length, out written);
        int err = Marshal.GetLastWin32Error();
        EndPagePrinter(h);
        if (!ok || written != data.Length) throw new Exception("WritePrinter wrote " + written + " of " + data.Length + ", error " + err);
      } finally { EndDocPrinter(h); }
    } finally { ClosePrinter(h); }
  }

  // A 4x6 label at 203 dots per inch: 812 x 1218 dots, one bit each (0 = black), as one TSPL BITMAP command.
  public static byte[] Tspl(Image src, int copies) {
    int W = 812, H = 1218;
    int rowBytes = (W + 7) / 8;
    byte[] bits = new byte[rowBytes * H];
    for (int i = 0; i < bits.Length; i++) bits[i] = 0xFF;
    using (Bitmap bmp = new Bitmap(W, H, PixelFormat.Format32bppArgb)) {
      using (Graphics g = Graphics.FromImage(bmp)) {
        g.Clear(Color.White);
        g.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.NearestNeighbor;
        g.PixelOffsetMode = System.Drawing.Drawing2D.PixelOffsetMode.Half;
        if (src.Width > src.Height) src.RotateFlip(RotateFlipType.Rotate90FlipNone);
        float s = Math.Min((float)W / src.Width, (float)H / src.Height);
        float w = src.Width * s, h = src.Height * s;
        g.DrawImage(src, (W - w) / 2f, (H - h) / 2f, w, h);
      }
      BitmapData bd = bmp.LockBits(new Rectangle(0, 0, W, H), ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
      try {
        byte[] px = new byte[bd.Stride * H];
        Marshal.Copy(bd.Scan0, px, 0, px.Length);
        for (int y = 0; y < H; y++) {
          int o = y * bd.Stride;
          for (int x = 0; x < W; x++) {
            int p = o + x * 4;
            int lum = (px[p] * 11 + px[p + 1] * 59 + px[p + 2] * 30) / 100;
            if (lum < 128) bits[y * rowBytes + (x >> 3)] &= (byte)(0xFF ^ (0x80 >> (x & 7)));
          }
        }
      } finally { bmp.UnlockBits(bd); }
    }
    byte[] head = Encoding.ASCII.GetBytes("SIZE 4,6\r\nCLS\r\nBITMAP 0,0," + rowBytes + "," + H + ",0,");
    byte[] tail = Encoding.ASCII.GetBytes("\r\nPRINT 1," + Math.Max(1, copies) + "\r\n");
    byte[] all = new byte[head.Length + bits.Length + tail.Length];
    Buffer.BlockCopy(head, 0, all, 0, head.Length);
    Buffer.BlockCopy(bits, 0, all, head.Length, bits.Length);
    Buffer.BlockCopy(tail, 0, all, head.Length + bits.Length, tail.Length);
    return all;
  }
}
"@
} catch { $script:RawErr = [string]$_ }
function Log([string]$m) {
  try {
    if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 2MB) { Move-Item -Force $Log ($Log + '.old') }
    Add-Content -Path $Log -Value ((Get-Date).ToString('MM-dd HH:mm:ss') + ' ' + $m)
  } catch {}
}
# One copy only.
$created = $false
$mutex = New-Object System.Threading.Mutex($true, 'Local\HubLabelPrinterAgent', [ref]$created)
if (-not $created) { exit }

# The label printer by name (Arkscan / 4BARCODE / 2054 / label / Zebra / TSC); the Windows default only if none
# matches. Jobs sent to the default printer went nowhere when the default was an office printer.
function Label-Printer {
  $all = @([System.Drawing.Printing.PrinterSettings]::InstalledPrinters)
  $hit = $all | Where-Object { $_ -match 'arkscan|4barcode|2054|label|thermal|zebra|tsc' -and $_ -notmatch 'pdf|xps|onenote|fax' } | Select-Object -First 1
  if ($hit) { return [string]$hit }
  return (New-Object System.Drawing.Printing.PrinterSettings).PrinterName
}

function Print-Picture([System.Drawing.Image]$img, [int]$copies) {
  # How long each part takes, reported with the job, so a slow driver shows up in the station's log.
  $script:psw = [System.Diagnostics.Stopwatch]::StartNew()
  $script:tPage = -1
  $script:tDraw = -1
  $script:pic = $img
  $script:left = [Math]::Max(1, $copies)
  $doc = New-Object System.Drawing.Printing.PrintDocument
  $doc.DocumentName = 'Hub label'
  $doc.PrinterSettings.PrinterName = $script:Printer
  # Copies: one page, the printer repeats it (drawing every copy as its own page made the printer stop and start).
  $script:left = 1
  if ($copies -gt 1 -and $doc.PrinterSettings.MaximumCopies -ge $copies) { $doc.PrinterSettings.Copies = [int16]$copies; $doc.PrinterSettings.Collate = $false }
  else { $script:left = [Math]::Max(1, $copies) }
  $doc.PrintController = New-Object System.Drawing.Printing.StandardPrintController
  $doc.DefaultPageSettings.Margins = New-Object System.Drawing.Printing.Margins(0, 0, 0, 0)
  $doc.add_PrintPage({
    param($sender, $e)
    if ($script:tPage -lt 0) { $script:tPage = $script:psw.ElapsedMilliseconds }
    $g = $e.Graphics
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor  # black/white dots, less for the driver
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::Half
    $pw = $e.PageBounds.Width; $ph = $e.PageBounds.Height
    $p = $script:pic
    # Turn a sideways picture to match the paper.
    if (($p.Width -gt $p.Height) -ne ($pw -gt $ph)) { $p.RotateFlip([System.Drawing.RotateFlipType]::Rotate90FlipNone) }
    $s = [Math]::Min($pw / $p.Width, $ph / $p.Height)
    $w = $p.Width * $s; $h = $p.Height * $s
    $x = ($pw - $w) / 2 - $e.PageSettings.HardMarginX
    $y = ($ph - $h) / 2 - $e.PageSettings.HardMarginY
    $g.DrawImage($p, [single]$x, [single]$y, [single]$w, [single]$h)
    $script:tDraw = $script:psw.ElapsedMilliseconds
    $script:left--
    $e.HasMorePages = ($script:left -gt 0)
  })
  $script:tSetup = $script:psw.ElapsedMilliseconds
  $doc.Print()
  $script:tEnd = $script:psw.ElapsedMilliseconds
  $doc.Dispose()
}

# A bag label the ops host couldn't draw: print the facts in big letters so the box still gets a label.
function Text-Picture($l) {
  $bmp = New-Object System.Drawing.Bitmap(812, 1218)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.Clear([System.Drawing.Color]::White)
  $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
  $b = [System.Drawing.Brushes]::Black
  $g.DrawString(('Chute: ' + $l.chute), (New-Object System.Drawing.Font('Arial', 36, [System.Drawing.FontStyle]::Bold)), $b, 50, 60)
  $g.DrawString([string]$l.route, (New-Object System.Drawing.Font('Arial', 60, [System.Drawing.FontStyle]::Bold)), $b, 50, 300)
  $g.DrawString([string]$l.bag, (New-Object System.Drawing.Font('Arial', 30, [System.Drawing.FontStyle]::Bold)), $b, 50, 800)
  $g.Dispose()
  return $bmp
}

$script:Printer = Label-Printer
Log ('started v' + $Version + ', printer: ' + $script:Printer + ' (all: ' + (@([System.Drawing.Printing.PrinterSettings]::InstalledPrinters) -join ', ') + ')')
$first = $true
while ($true) {
  [void][HubAwake]::SetThreadExecutionState([uint32]2147483649)  # keep the laptop awake (screen may turn off)
  try {
    $wait = if ($first) { 0 } else { 25 }
    $d = Invoke-RestMethod -Uri ($Base + '/next?agent=1&wait=' + $wait + '&v=' + $Version + '&pr=' + [Uri]::EscapeDataString([string]$script:Printer + ' ; all: ' + (@([System.Drawing.Printing.PrinterSettings]::InstalledPrinters) -join ', '))) -TimeoutSec 60 -UseBasicParsing
    $first = $false
    # A newer agent on the station: download it, start it, and stop this copy.
    if ($d.agentVersion -and $d.agentVersion -ne $Version -and -not @($d.queue).Count) {
      try {
        $new = Join-Path $Dir 'agent.ps1.new'
        Invoke-WebRequest -UseBasicParsing ($Base + '/agent.ps1') -OutFile $new -TimeoutSec 60
        # Never swap in a script that does not parse: a broken copy would stop printing until someone reinstalls.
        $tokens = $null
        $errs = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($new, [ref]$tokens, [ref]$errs)
        if ($errs -and $errs.Count -gt 0) { Log ('update v' + $d.agentVersion + ' does not parse, kept this one: ' + $errs[0]) }
        elseif ((Get-Item $new).Length -gt 1000) {
          Move-Item -Force $new (Join-Path $Dir 'agent.ps1')
          Log ('updating to v' + $d.agentVersion)
          $mutex.ReleaseMutex(); $mutex.Dispose()
          Start-Process wscript.exe -ArgumentList ('"' + (Join-Path $Dir 'run.vbs') + '"')
          exit
        }
      } catch { Log ('update failed: ' + $_) }
    }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $script:Printer = Label-Printer
    $tPick = $sw.ElapsedMilliseconds
    foreach ($j in @($d.queue)) {
      if (-not $j) { continue }
      $l = $j.label
      try {
        $t0 = $sw.ElapsedMilliseconds
        $copies = if ($l.copies) { [int]$l.copies } else { 1 }
        if ($l.img) {
          $bytes = [Convert]::FromBase64String($l.img.Substring($l.img.IndexOf(',') + 1))
          $ms = New-Object System.IO.MemoryStream(,$bytes)
          $img = [System.Drawing.Image]::FromStream($ms)
        } else { $ms = $null; $img = Text-Picture $l }
        $info = 'sent to ' + $script:Printer + ' (' + $img.Width + 'x' + $img.Height + ')'
        $t1 = $sw.ElapsedMilliseconds
        $rawOk = $false
        if ($l.raw) {
          try {
            $data = [HubRaw]::Tspl($img, $copies)
            $tMade = $sw.ElapsedMilliseconds
            [HubRaw]::Send($script:Printer, $data)
            $rawOk = $true
            $info = 'RAW sent to ' + $script:Printer + ' (' + $data.Length + ' bytes, made in ' + ($tMade - $t1) + ' ms, sent in ' + ($sw.ElapsedMilliseconds - $tMade) + ' ms)'
          } catch { $info = 'raw failed [' + $_ + '] [' + $script:RawErr + '] so drawn the old way: ' + $info }
        }
        if (-not $rawOk) { Print-Picture $img $copies }
        $t2 = $sw.ElapsedMilliseconds
        $img.Dispose(); if ($ms) { $ms.Dispose() }
        try {
          $q = @(Get-PrintJob -PrinterName $script:Printer -ErrorAction Stop)
          $pst = Get-Printer -Name $script:Printer -ErrorAction Stop
          $info += ' - printer ' + $pst.PrinterStatus + ' - queue ' + $q.Count + ': ' + (($q | ForEach-Object { $_.JobStatus }) -join ',')
          $info += ' | driver ' + $pst.DriverName + ' on ' + $pst.PortName
        } catch { $info += ' - queue unknown' }
        $info += ' | ms: find printer ' + $tPick + ', picture ' + ($t1 - $t0) + ', print ' + ($t2 - $t1) + ' (setup ' + $script:tSetup + ', page ' + $script:tPage + ', drawn ' + $script:tDraw + ', end ' + $script:tEnd + '), queue check ' + ($sw.ElapsedMilliseconds - $t2) + ', since job arrived ' + $sw.ElapsedMilliseconds
        Log ('printed ' + $l.route + ' ' + $l.bag + ' x' + $copies + ' ' + $info)
      } catch { $info = 'PRINT FAILED on ' + $script:Printer + ': ' + $_; Log $info }
      try { Invoke-RestMethod -Uri ($Base + '/printed') -Method Post -ContentType 'application/json' -Body (ConvertTo-Json @{ id = $j.id; info = [string]$info } -Compress) -TimeoutSec 30 -UseBasicParsing | Out-Null } catch {}
    }
  } catch {
    Log ('no connection: ' + $_)
    Start-Sleep -Seconds 5
  }
}
