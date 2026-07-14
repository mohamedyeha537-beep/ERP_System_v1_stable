# Disables QuickEdit on the current console — clicking the window otherwise freezes the server.
$sig = @'
using System;
using System.Runtime.InteropServices;
public class PosConsole {
  [DllImport("kernel32.dll")] public static extern IntPtr GetStdHandle(int n);
  [DllImport("kernel32.dll")] public static extern bool GetConsoleMode(IntPtr h, out uint m);
  [DllImport("kernel32.dll")] public static extern bool SetConsoleMode(IntPtr h, uint m);
}
'@
Add-Type -TypeDefinition $sig -ErrorAction SilentlyContinue | Out-Null
$h = [PosConsole]::GetStdHandle(-10)
$m = 0u
if ([PosConsole]::GetConsoleMode($h, [ref]$m)) {
    [PosConsole]::SetConsoleMode($h, ($m -band (-bnot 0x0040))) | Out-Null
}
