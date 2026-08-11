using AccessibleVideoToText.App;
using AccessibleVideoToText.Core;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class WinFormsAccessibilityBaselineTests
{
    [TestMethod]
    public void MainWindow_UsesNamedStandardControlsAndDpiScaling()
    {
        RunInSta(() =>
        {
            using var form = new MainForm();
            Assert.AreEqual("飞船下载转换工具", form.Text);
            Assert.AreEqual(AutoScaleMode.Dpi, form.AutoScaleMode);
            Assert.IsTrue(form.KeyPreview);

            var controls = Descendants(form).ToArray();
            var linkInput = controls.OfType<TextBox>().Single(textBox =>
                textBox.AccessibleName == "下载链接或平台分享文本");
            Assert.AreEqual(0, linkInput.TabIndex);
            var mode = controls.OfType<ComboBox>().Single();
            Assert.AreEqual(ComboBoxStyle.DropDownList, mode.DropDownStyle);
            Assert.AreEqual(1, mode.TabIndex);
            CollectionAssert.AreEqual(
                new[] { "只下载", "下载后转换为 MP3", "下载、转换 MP3 并生成 TXT" },
                mode.Items.Cast<string>().ToArray());
            Assert.AreEqual(0, mode.SelectedIndex);
            mode.SelectedIndex = 2;
            var resetMode = typeof(MainForm).GetMethod(
                "ResetProcessingMode",
                BindingFlags.Instance | BindingFlags.NonPublic);
            Assert.IsNotNull(resetMode);
            resetMode.Invoke(form, null);
            Assert.AreEqual(0, mode.SelectedIndex);

            var tabs = controls.OfType<TabControl>().Single();
            Assert.AreEqual(2, tabs.TabPages.Count);
            Assert.AreEqual("任务队列", tabs.TabPages[0].Text);
            Assert.AreEqual("状态与日志", tabs.TabPages[1].Text);
            var queue = controls.OfType<ListView>().Single();
            Assert.AreEqual(View.Details, queue.View);
            Assert.IsFalse(queue.OwnerDraw);
            Assert.AreEqual("文件队列", queue.AccessibleName);
            Assert.IsFalse(queue.HideSelection);

            var progress = controls.OfType<ProgressBar>().Single();
            Assert.AreEqual("当前任务进度", progress.AccessibleName);
            var results = controls.OfType<TextBox>().Single(textBox => textBox.Multiline && textBox.ReadOnly);
            Assert.AreEqual("状态与日志", results.AccessibleName);

            foreach (var button in controls.OfType<Button>())
            {
                Assert.IsFalse(string.IsNullOrWhiteSpace(button.AccessibleName ?? button.Text));
                Assert.IsNull(button.Image, $"按钮 {button.Text} 不得使用图片作为操作。 ");
            }
        });
    }

    [TestMethod]
    public void ConfirmationAndCancellationDialogs_HaveSafeDefaultButtons()
    {
        RunInSta(() =>
        {
            using var paste = new PasteConfirmationDialog([
                new QueueItem(@"D:\媒体\一.mp4", MediaKind.Video),
                new QueueItem(@"D:\媒体\二.mp3", MediaKind.Mp3)]);
            Assert.IsNotNull(paste.AcceptButton);
            Assert.AreEqual("处理全部", ((Button)paste.AcceptButton).Text);
            Assert.IsNotNull(paste.CancelButton);

            using var cancellation = new CancellationScopeDialog(cloudTaskAlreadySubmitted: false);
            Assert.IsNotNull(cancellation.AcceptButton);
            Assert.AreEqual("继续处理", ((Button)cancellation.AcceptButton).Text);
            Assert.AreSame(cancellation.AcceptButton, cancellation.CancelButton);

            using var trayExit = new TrayExitDialog();
            Assert.IsNotNull(trayExit.AcceptButton);
            Assert.AreEqual("转入托盘继续（默认）", ((Button)trayExit.AcceptButton).Text);
            Assert.IsNotNull(trayExit.CancelButton);
            Assert.AreEqual("返回软件", ((Button)trayExit.CancelButton).Text);
            CollectionAssert.AreEquivalent(
                new[] { "转入托盘继续（默认）", "取消任务并退出", "返回软件" },
                Descendants(trayExit).OfType<Button>().Select(button => button.Text).ToArray());
        });
    }

    [TestMethod]
    public void CloudCredentialsDialog_UsesMaskedStandardInputsAndExplicitAcknowledgement()
    {
        RunInSta(() =>
        {
            using var dialog = new CloudCredentialsDialog();
            Assert.AreEqual(AutoScaleMode.Dpi, dialog.AutoScaleMode);
            Assert.IsNotNull(dialog.AcceptButton);
            var save = (Button)dialog.AcceptButton;
            Assert.AreEqual("仅保存配置", save.Text);
            Assert.IsFalse(save.Enabled, "未确认费用和旧密钥撤销前不得保存。 ");
            Assert.IsNotNull(dialog.CancelButton);

            var controls = Descendants(dialog).ToArray();
            var secretKey = controls.OfType<TextBox>().Single(textBox => textBox.AccessibleName == "腾讯云 SecretKey");
            Assert.IsTrue(secretKey.UseSystemPasswordChar);
            Assert.IsTrue(controls.OfType<CheckBox>().Any(checkBox =>
                checkBox.Text.Contains("隐私", StringComparison.Ordinal) &&
                checkBox.Text.Contains("费用", StringComparison.Ordinal)));
            Assert.IsTrue(controls.All(control => control.GetType() != typeof(UserControl)), "云端配置必须使用标准 WinForms 控件。 ");
        });
    }

    [TestMethod]
    public void HelpDialog_UsesStandardReadOnlyTextAndSafeCloseButton()
    {
        RunInSta(() =>
        {
            using var dialog = new HelpDialog("使用说明测试");
            Assert.AreEqual(AutoScaleMode.Dpi, dialog.AutoScaleMode);
            var controls = Descendants(dialog).ToArray();
            var content = controls.OfType<TextBox>().Single();
            Assert.IsTrue(content.Multiline);
            Assert.IsTrue(content.ReadOnly);
            Assert.AreEqual("使用说明正文", content.AccessibleName);
            Assert.IsNotNull(dialog.AcceptButton);
            Assert.AreEqual("关闭", ((Button)dialog.AcceptButton).Text);
            Assert.AreSame(dialog.AcceptButton, dialog.CancelButton);
        });
    }

    [TestMethod]
    public void CompletedState_DisablesCancelAndDoesNotBlockIdleClose()
    {
        RunInSta(() =>
        {
            using var form = new MainForm();
            var processing = typeof(MainForm).GetProperty(
                "IsProcessing",
                BindingFlags.Instance | BindingFlags.NonPublic);
            var previousFinished = typeof(MainForm).GetField(
                "previousBatchFinished",
                BindingFlags.Instance | BindingFlags.NonPublic);
            var updateButtons = typeof(MainForm).GetMethod(
                "UpdateButtons",
                BindingFlags.Instance | BindingFlags.NonPublic);
            var handleClosing = typeof(MainForm).GetMethod(
                "HandleFormClosing",
                BindingFlags.Instance | BindingFlags.NonPublic);
            Assert.IsNotNull(processing);
            Assert.IsNotNull(previousFinished);
            Assert.IsNotNull(updateButtons);
            Assert.IsNotNull(handleClosing);

            processing.SetValue(form, false);
            previousFinished.SetValue(form, true);
            updateButtons.Invoke(form, null);
            var cancel = Descendants(form).OfType<Button>().Single(button => button.Text.Contains("取消"));
            Assert.IsFalse(cancel.Enabled, "任务结束后取消按钮不得继续处于阻塞状态。 ");

            var closing = new FormClosingEventArgs(CloseReason.UserClosing, cancel: false);
            handleClosing.Invoke(form, [form, closing]);
            Assert.IsFalse(closing.Cancel, "空闲或已完成状态下 Alt+F4/关闭必须直接退出。 ");
        });
    }

    [TestMethod]
    public void NumpadEight_AnnouncesOneCurrentProgressSnapshotPerPress()
    {
        RunInSta(() =>
        {
            using var form = new MainForm();
            form.CreateControl();
            var processing = typeof(MainForm).GetProperty(
                "IsProcessing",
                BindingFlags.Instance | BindingFlags.NonPublic);
            var activity = typeof(MainForm).GetField(
                "currentTaskActivity",
                BindingFlags.Instance | BindingFlags.NonPublic);
            var percentage = typeof(MainForm).GetField(
                "currentTaskPercentage",
                BindingFlags.Instance | BindingFlags.NonPublic);
            var announcement = typeof(MainForm).GetField(
                "lastProgressAnnouncement",
                BindingFlags.Instance | BindingFlags.NonPublic);
            var requestCount = typeof(MainForm).GetField(
                "progressAnnouncementRequestCount",
                BindingFlags.Instance | BindingFlags.NonPublic);
            var processKey = typeof(MainForm).GetMethod(
                "ProcessCmdKey",
                BindingFlags.Instance | BindingFlags.NonPublic);
            var setProgress = typeof(MainForm).GetMethod(
                "SetCurrentTaskProgress",
                BindingFlags.Instance | BindingFlags.NonPublic);
            Assert.IsNotNull(processing);
            Assert.IsNotNull(activity);
            Assert.IsNotNull(percentage);
            Assert.IsNotNull(announcement);
            Assert.IsNotNull(requestCount);
            Assert.IsNotNull(processKey);
            Assert.IsNotNull(setProgress);

            processing.SetValue(form, true);
            setProgress.Invoke(form, ["正在下载视频", 42]);
            Assert.AreEqual("飞船下载转换工具，当前任务进度 42%", form.Text);
            Assert.AreEqual("飞船下载转换工具，当前任务进度 42%，主窗口", form.AccessibleName);
            var numpadMessage = Message.Create(
                form.Handle,
                0x0100,
                (IntPtr)(int)Keys.NumPad8,
                (IntPtr)(0x48 << 16));
            var handled = (bool)processKey.Invoke(form, [numpadMessage, Keys.NumPad8])!;
            Assert.IsTrue(handled);
            Assert.AreEqual(1, requestCount.GetValue(form));
            Assert.AreEqual("正在下载视频，进度 42%。", announcement.GetValue(form));

            // Num Lock 关闭时同一物理键表现为非扩展 VK_UP，仍应播报。
            percentage.SetValue(form, null);
            activity.SetValue(form, "正在合并音视频");
            var numLockOffMessage = Message.Create(
                form.Handle,
                0x0100,
                (IntPtr)(int)Keys.Up,
                (IntPtr)(0x48 << 16));
            handled = (bool)processKey.Invoke(form, [numLockOffMessage, Keys.Up])!;
            Assert.IsTrue(handled);
            Assert.AreEqual(2, requestCount.GetValue(form));
            Assert.AreEqual(
                "正在合并音视频，暂时没有可用百分比。",
                announcement.GetValue(form));

            // 独立方向键上带扩展位，不能被误认为小键盘 8。
            var arrowMessage = Message.Create(
                form.Handle,
                0x0100,
                (IntPtr)(int)Keys.Up,
                (IntPtr)((0x48 << 16) | (1 << 24)));
            processKey.Invoke(form, [arrowMessage, Keys.Up]);
            Assert.AreEqual(2, requestCount.GetValue(form));

            processing.SetValue(form, false);
            processKey.Invoke(form, [numpadMessage, Keys.NumPad8]);
            Assert.AreEqual(2, requestCount.GetValue(form));
            Assert.AreEqual("飞船下载转换工具", form.Text);
            Assert.AreEqual("飞船下载转换工具主窗口", form.AccessibleName);
        });
    }

    [TestMethod]
    public void ProductAssemblyVersions_AreFixedAtOnePointZero()
    {
        var assembly = typeof(MainForm).Assembly;
        Assert.AreEqual(new Version(1, 0, 0, 0), assembly.GetName().Version);
        var informational = assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>();
        Assert.IsNotNull(informational);
        Assert.AreEqual("1.0", informational.InformationalVersion);
    }

    [TestMethod]
    public void InstalledHelpSource_IsUtf8BomWithCrLfAndRequiredSections()
    {
        var current = new DirectoryInfo(Environment.CurrentDirectory);
        string? path = null;
        while (current is not null)
        {
            var candidate = Path.Combine(current.FullName, "docs", "release", "使用说明.txt");
            if (File.Exists(candidate))
            {
                path = candidate;
                break;
            }

            current = current.Parent;
        }

        Assert.IsNotNull(path);
        var bytes = File.ReadAllBytes(path);
        Assert.IsTrue(bytes.Length > 3);
        CollectionAssert.AreEqual(new byte[] { 0xEF, 0xBB, 0xBF }, bytes[..3]);
        var text = Encoding.UTF8.GetString(bytes);
        StringAssert.Contains(text, "\r\n");
        Assert.IsFalse(Regex.IsMatch(text, "(?<!\\r)\\n"));
        foreach (var required in new[]
                 {
                     "Ctrl+V", "只下载", "下载后转换为 MP3", "下载、转换 MP3 并生成 TXT",
                     "抖音专用登录", "小键盘 8", "DPAPI CurrentUser", "每月 10 小时", "COS"
                 })
        {
            StringAssert.Contains(text, required);
        }
    }

    private static IEnumerable<Control> Descendants(Control root)
    {
        foreach (Control child in root.Controls)
        {
            yield return child;
            foreach (var descendant in Descendants(child))
            {
                yield return descendant;
            }
        }
    }

    private static void RunInSta(Action action)
    {
        Exception? exception = null;
        using var completed = new ManualResetEventSlim();
        var thread = new Thread(() =>
        {
            try
            {
                action();
            }
            catch (Exception caught)
            {
                exception = caught;
            }
            finally
            {
                completed.Set();
            }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        Assert.IsTrue(completed.Wait(TimeSpan.FromSeconds(15)), "STA 无障碍基线检查超时。 ");
        thread.Join();
        if (exception is not null)
        {
            throw new AssertFailedException($"STA 无障碍基线检查失败：{exception.Message}", exception);
        }
    }
}
