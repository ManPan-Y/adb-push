using Microsoft.UI.Xaml;

namespace YHGamePush;

public partial class App : Application
{
    private Window? _window;

    public App()
    {
        AppDomain.CurrentDomain.UnhandledException += (_, e) => Dump(e.ExceptionObject as Exception);
        UnhandledException += (_, e) => Dump(e.Exception);
        try
        {
            InitializeComponent();
        }
        catch (Exception ex)
        {
            Dump(ex);
            throw;
        }
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        try
        {
            _window = new MainWindow();
            _window.Activate();
        }
        catch (Exception ex)
        {
            Dump(ex);
            throw;
        }
    }

    private static void Dump(Exception? ex)
    {
        try
        {
            File.AppendAllText(Path.Combine(AppContext.BaseDirectory, "crash.log"),
                $"[{DateTime.Now:O}]\n{ex}\n\n");
        }
        catch { }
    }
}
