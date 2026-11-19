// Runs a program on a separate desktop that is never switched to, so none of its windows
// (or the windows of COM servers it starts) can appear on the owner's screen or take keystrokes.
// usage: deskrun.exe <desktopName> <exe> [args...]   -> waits for the program, returns its exit code
using System;
using System.Runtime.InteropServices;

static class DeskRun
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct STARTUPINFO { public int cb; public string lpReserved; public string lpDesktop; public string lpTitle; public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags; public short wShowWindow, cbReserved2; public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError; }
    [StructLayout(LayoutKind.Sequential)]
    struct PROCESS_INFORMATION { public IntPtr hProcess, hThread; public int dwProcessId, dwThreadId; }
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr CreateDesktop(string name, IntPtr dev, IntPtr mode, int flags, uint access, IntPtr sa);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CreateProcess(string app, string cmd, IntPtr pa, IntPtr ta, bool inherit, uint flags, IntPtr env, string cwd, ref STARTUPINFO si, out PROCESS_INFORMATION pi);
    [DllImport("kernel32.dll")]
    static extern uint WaitForSingleObject(IntPtr h, uint ms);
    [DllImport("kernel32.dll")]
    static extern bool GetExitCodeProcess(IntPtr h, out uint code);

    static int Main(string[] a)
    {
        IntPtr d = CreateDesktop(a[0], IntPtr.Zero, IntPtr.Zero, 0, 0x10000000u /*GENERIC_ALL*/, IntPtr.Zero);
        if (d == IntPtr.Zero) { Console.Error.WriteLine("CreateDesktop failed " + Marshal.GetLastWin32Error()); return 101; }
        var si = new STARTUPINFO(); si.cb = Marshal.SizeOf(si); si.lpDesktop = "WinSta0\\" + a[0];
        string cmd = "\"" + a[1] + "\"" + (a.Length > 2 ? " " + string.Join(" ", a, 2, a.Length - 2) : "");
        PROCESS_INFORMATION pi;
        if (!CreateProcess(null, cmd, IntPtr.Zero, IntPtr.Zero, false, 0x08000000 /*CREATE_NO_WINDOW*/, IntPtr.Zero, null, ref si, out pi))
        { Console.Error.WriteLine("CreateProcess failed " + Marshal.GetLastWin32Error()); return 102; }
        Console.WriteLine("pid " + pi.dwProcessId);
        Console.Out.Flush();
        // The desktop must stay open while the child runs: if this launcher exits first, the desktop is destroyed.
        WaitForSingleObject(pi.hProcess, 0xFFFFFFFF);
        uint code; GetExitCodeProcess(pi.hProcess, out code);
        return (int)code;
    }
}
