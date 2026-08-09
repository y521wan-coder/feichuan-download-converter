using AccessibleVideoToText.Core;
using AccessibleVideoToText.Infrastructure;

namespace AccessibleVideoToText.App;

public sealed class MainForm : Form
{
    private readonly ClipboardBatchAnalyzer clipboardAnalyzer = new();
    private readonly LocalDataPaths localDataPaths;
    private readonly DpapiSettingsStore settingsStore;
    private readonly IMediaProbe mediaProbe;
    private readonly IAudioConverter audioConverter;
    private readonly IAtomicOutputCommitter outputCommitter;
    private readonly JsonJobStore jobStore;
    private readonly JsonUsageLedger usageLedger;
    private readonly LocalVideoProcessor localVideoProcessor;
    private readonly ListView queueList = new();
    private readonly Label statusLabel = new();
    private readonly ProgressBar progressBar = new();
    private readonly TextBox resultText = new();
    private readonly Button startButton = new();
    private readonly Button removeButton = new();
    private readonly Button cancelButton = new();
    private readonly Button outputButton = new();
    private readonly OpenFileDialog openFileDialog = new();
    private readonly NotifyIcon trayIcon = new();
    private CancellationTokenSource? batchCancellation;
    private CancellationTokenSource? currentItemCancellation;
    private TaskCompletionSource? batchCompletion;
    private SystemSleepInhibitor? sleepInhibitor;
    private AppSettings settings = new();
    private bool skipCurrentRequested;
    private bool stopBatchRequested;
    private bool exitRequested;
    private bool cloudTaskAlreadySubmitted;
    private bool IsProcessing { get; set; }
    private bool previousBatchFinished;

    public MainForm()
    {
        Text = "无障碍视频转文字";
        AccessibleName = "无障碍视频转文字主窗口";
        StartPosition = FormStartPosition.CenterScreen;
        MinimumSize = new Size(760, 560);
        Size = new Size(920, 680);
        KeyPreview = true;
        AutoScaleMode = AutoScaleMode.Dpi;

        localDataPaths = new LocalDataPaths();
        settingsStore = new DpapiSettingsStore(localDataPaths);
        mediaProbe = new FfmpegMediaProbe(localDataPaths);
        audioConverter = new FfmpegAudioConverter(localDataPaths);
        outputCommitter = new AtomicOutputCommitter();
        jobStore = new JsonJobStore(localDataPaths);
        usageLedger = new JsonUsageLedger(localDataPaths);
        localVideoProcessor = new LocalVideoProcessor(
            mediaProbe,
            audioConverter,
            outputCommitter);

        MainMenuStrip = BuildMenu();
        Controls.Add(BuildLayout());
        Controls.Add(MainMenuStrip);

        openFileDialog.Title = "选择视频或 MP3";
        openFileDialog.Multiselect = true;
        openFileDialog.CheckFileExists = true;
        openFileDialog.Filter = "视频和 MP3|*.mp4;*.mkv;*.mov;*.avi;*.wmv;*.flv;*.webm;*.m4v;*.mpeg;*.mpg;*.ts;*.m2ts;*.3gp;*.mp3|所有文件|*.*";

        ConfigureTrayIcon();
        Resize += (_, _) =>
        {
            if (WindowState == FormWindowState.Minimized)
            {
                HideToTray();
            }
        };
        FormClosing += HandleFormClosing;
        FormClosed += (_, _) =>
        {
            trayIcon.Visible = false;
            trayIcon.Dispose();
            sleepInhibitor?.Dispose();
        };

        Shown += async (_, _) =>
        {
            await LoadSettingsAsync();
            queueList.Focus();
            await PromptForPendingRecoveryAsync();
        };
    }

    protected override bool ProcessCmdKey(ref Message msg, Keys keyData)
    {
        if (keyData == (Keys.Control | Keys.V))
        {
            PasteClipboardFiles();
            return true;
        }

        if (keyData == Keys.Delete && queueList.ContainsFocus)
        {
            RemoveSelectedQueueItems();
            return true;
        }

        if (keyData == Keys.F1)
        {
            ShowHelp();
            return true;
        }

        if (keyData == Keys.Escape && IsProcessing)
        {
            ShowCancellationScopeDialog();
            return true;
        }

        return base.ProcessCmdKey(ref msg, keyData);
    }

    private MenuStrip BuildMenu()
    {
        var menu = new MenuStrip
        {
            AccessibleName = "主菜单"
        };

        var fileMenu = new ToolStripMenuItem("文件(&F)");
        var openItem = new ToolStripMenuItem("添加文件(&O)", null, (_, _) => OpenFiles())
        {
            ShortcutKeys = Keys.Control | Keys.O
        };
        var exitItem = new ToolStripMenuItem("退出(&X)", null, (_, _) => Close());
        fileMenu.DropDownItems.AddRange([openItem, new ToolStripSeparator(), exitItem]);

        var actionMenu = new ToolStripMenuItem("操作(&A)");
        var pasteItem = new ToolStripMenuItem("粘贴资源管理器文件(&V)", null, (_, _) => PasteClipboardFiles())
        {
            ShortcutKeys = Keys.Control | Keys.V
        };
        var settingsItem = new ToolStripMenuItem("设置(&S)", null, (_, _) => ShowSettingsPlaceholder())
        {
            ShortcutKeys = Keys.Alt | Keys.S
        };
        var outputItem = new ToolStripMenuItem("选择或打开结果目录(&O)", null, (_, _) => ShowOutputPlaceholder())
        {
            ShortcutKeys = Keys.Alt | Keys.O
        };
        actionMenu.DropDownItems.AddRange([pasteItem, settingsItem, outputItem]);

        var helpMenu = new ToolStripMenuItem("帮助(&H)");
        var keyboardHelp = new ToolStripMenuItem("键盘和云端帮助(&H)", null, (_, _) => ShowHelp())
        {
            ShortcutKeys = Keys.F1
        };
        helpMenu.DropDownItems.Add(keyboardHelp);

        menu.Items.AddRange([fileMenu, actionMenu, helpMenu]);
        return menu;
    }

    private Control BuildLayout()
    {
        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(12),
            ColumnCount = 1,
            RowCount = 8
        };
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 62));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 38));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));

        var queueLabel = new Label
        {
            Text = "文件队列（Ctrl+V 粘贴资源管理器中复制的文件）",
            AutoSize = true,
            AccessibleName = "文件队列说明"
        };

        queueList.Dock = DockStyle.Fill;
        queueList.View = View.Details;
        queueList.FullRowSelect = true;
        queueList.MultiSelect = true;
        queueList.HideSelection = false;
        queueList.UseCompatibleStateImageBehavior = false;
        queueList.AccessibleName = "文件队列";
        queueList.AccessibleDescription = "显示文件名、类型、状态、当前步骤和结果。按 Delete 只从队列移除，不删除磁盘文件。";
        queueList.Columns.Add("文件名", 280);
        queueList.Columns.Add("类型", 95);
        queueList.Columns.Add("状态", 100);
        queueList.Columns.Add("当前步骤和结果", 360);
        queueList.SelectedIndexChanged += (_, _) => UpdateButtons();

        statusLabel.Text = "状态：空闲。请按 Ctrl+V 粘贴文件，或按 Ctrl+O 添加文件。";
        statusLabel.AutoSize = true;
        statusLabel.Margin = new Padding(0, 10, 0, 4);
        statusLabel.AccessibleName = "当前任务状态";
        statusLabel.AccessibleDescription = statusLabel.Text;

        progressBar.Dock = DockStyle.Top;
        progressBar.Minimum = 0;
        progressBar.Maximum = 100;
        progressBar.AccessibleName = "当前任务进度";
        progressBar.AccessibleDescription = "当前没有任务，进度 0%。";

        var buttonPanel = new FlowLayoutPanel
        {
            AutoSize = true,
            Dock = DockStyle.Fill,
            FlowDirection = FlowDirection.LeftToRight,
            WrapContents = true,
            Margin = new Padding(0, 8, 0, 8)
        };

        startButton.Text = "开始处理(&B)";
        startButton.AutoSize = true;
        startButton.Enabled = false;
        startButton.Click += (_, _) => ConfirmQueuedBatch();

        removeButton.Text = "从队列移除(&R)";
        removeButton.AutoSize = true;
        removeButton.Enabled = false;
        removeButton.Click += (_, _) => RemoveSelectedQueueItems();

        cancelButton.Text = "取消任务(&C)";
        cancelButton.AutoSize = true;
        cancelButton.Enabled = false;
        cancelButton.Click += (_, _) => ShowCancellationScopeDialog();

        outputButton.Text = "结果目录(&O)";
        outputButton.AutoSize = true;
        outputButton.Click += (_, _) => ShowOutputPlaceholder();

        buttonPanel.Controls.AddRange([startButton, removeButton, cancelButton, outputButton]);

        var resultLabel = new Label
        {
            Text = "本批结果",
            AutoSize = true,
            AccessibleName = "本批结果标题"
        };

        resultText.Dock = DockStyle.Fill;
        resultText.Multiline = true;
        resultText.ReadOnly = true;
        resultText.ScrollBars = ScrollBars.Vertical;
        resultText.AccessibleName = "本批结果";
        resultText.AccessibleDescription = "可重新查看本批成功、失败、保存位置和数量。";
        resultText.Text = "尚未处理任何批次。";

        var privacyLabel = new Label
        {
            AutoSize = true,
            Text = "隐私提示：文件名可显示；读屏和日志不会自动朗读或记录完整路径。云端功能使用前会单独说明费用与隐私。",
            AccessibleName = "隐私提示"
        };

        layout.Controls.Add(queueLabel, 0, 0);
        layout.Controls.Add(queueList, 0, 1);
        layout.Controls.Add(statusLabel, 0, 2);
        layout.Controls.Add(progressBar, 0, 3);
        layout.Controls.Add(buttonPanel, 0, 4);
        layout.Controls.Add(resultLabel, 0, 5);
        layout.Controls.Add(resultText, 0, 6);
        layout.Controls.Add(privacyLabel, 0, 7);
        return layout;
    }

    private void OpenFiles()
    {
        if (IsProcessing)
        {
            ReportStatus("正在处理，不能添加新文件。请等待批次结束或取消任务。", true);
            return;
        }

        if (openFileDialog.ShowDialog(this) == DialogResult.OK)
        {
            IntakeFiles(openFileDialog.FileNames);
        }
        else
        {
            queueList.Focus();
        }
    }

    private void PasteClipboardFiles()
    {
        if (IsProcessing)
        {
            ReportStatus("正在处理，已拒绝新粘贴。请等待批次结束或取消任务。", true);
            return;
        }

        if (!Clipboard.ContainsFileDropList())
        {
            ReportStatus("剪贴板中没有资源管理器文件。请先在资源管理器复制视频或 MP3。", true);
            return;
        }

        var files = Clipboard.GetFileDropList().Cast<string>().ToArray();
        IntakeFiles(files);
    }

    private void IntakeFiles(IEnumerable<string> paths)
    {
        var result = clipboardAnalyzer.Analyze(paths);
        if (result.RejectedForLimit || result.Accepted.Count == 0)
        {
            ReportStatus(result.Summary, true);
            MessageBox.Show(this, result.Summary, "没有加入文件", MessageBoxButtons.OK, MessageBoxIcon.Information);
            queueList.Focus();
            return;
        }

        if (previousBatchFinished)
        {
            queueList.Items.Clear();
            resultText.Text = "尚未处理当前批次。";
            previousBatchFinished = false;
        }

        foreach (var item in result.Accepted)
        {
            var row = CreateQueueRow(item);
            queueList.Items.Add(row);
        }

        queueList.Items[0].Selected = true;
        queueList.Items[0].Focused = true;
        ReportStatus(result.Summary, false);
        UpdateButtons();
        ConfirmQueuedBatch();
    }

    private static ListViewItem CreateQueueRow(QueueItem item)
    {
        var row = new ListViewItem(item.FileName)
        {
            Tag = item,
            ToolTipText = $"{item.FileName}，{item.KindText}，{item.Stage.ToAccessibleText()}"
        };
        row.SubItems.Add(item.KindText);
        row.SubItems.Add(item.Stage.ToAccessibleText());
        row.SubItems.Add(item.StepDetail);
        return row;
    }

    private async void ConfirmQueuedBatch()
    {
        if (IsProcessing || queueList.Items.Count == 0)
        {
            return;
        }

        var items = queueList.Items.Cast<ListViewItem>()
            .Select(row => row.Tag)
            .OfType<QueueItem>()
            .ToArray();

        using var dialog = new PasteConfirmationDialog(items);
        var trigger = ActiveControl;
        if (dialog.ShowDialog(this) == DialogResult.OK && dialog.Decision is not null)
        {
            var decision = dialog.Decision;
            if (decision.TranscribeVideos || decision.TranscribeMp3)
            {
                ReportStatus(decision.Describe(), false);
                await StartCloudBatchAsync(decision);
            }
            else
            {
                ReportStatus(decision.Describe(), false);
                await StartLocalBatchAsync(decision);
            }
        }

        if (trigger is not null && trigger.CanFocus)
        {
            trigger.Focus();
        }
        else if (!IsProcessing)
        {
            queueList.Focus();
        }
    }

    private void RemoveSelectedQueueItems()
    {
        if (IsProcessing || queueList.SelectedIndices.Count == 0)
        {
            return;
        }

        var nextIndex = queueList.SelectedIndices.Cast<int>().Min();
        var selected = queueList.SelectedItems.Cast<ListViewItem>().ToArray();
        foreach (var item in selected)
        {
            queueList.Items.Remove(item);
        }

        if (queueList.Items.Count > 0)
        {
            nextIndex = Math.Min(nextIndex, queueList.Items.Count - 1);
            queueList.Items[nextIndex].Selected = true;
            queueList.Items[nextIndex].Focused = true;
        }

        ReportStatus($"已从队列移除 {selected.Length} 项。磁盘文件没有删除。", false);
        UpdateButtons();
        queueList.Focus();
    }

    private void UpdateButtons()
    {
        startButton.Enabled = !IsProcessing && queueList.Items.Count > 0;
        removeButton.Enabled = !IsProcessing && queueList.SelectedItems.Count > 0;
        cancelButton.Enabled = IsProcessing;
    }

    private async Task LoadSettingsAsync()
    {
        try
        {
            settings = await settingsStore.LoadAsync(CancellationToken.None);
            if (settings.Mp3BitrateKbps is not (128 or 192 or 256 or 320))
            {
                settings = new AppSettings();
            }
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or System.Text.Json.JsonException)
        {
            settings = new AppSettings();
            ReportStatus("设置文件无法读取，已在本次运行使用默认 192 kbps；没有覆盖原设置。", true);
        }
    }

    private async Task StartLocalBatchAsync(PasteDecision decision)
    {
        var allItems = queueList.Items.Cast<ListViewItem>()
            .Select(row => row.Tag)
            .OfType<QueueItem>()
            .ToArray();
        var scopedItems = decision.FirstItemOnly ? allItems.Take(1).ToArray() : allItems;
        if (scopedItems.Length == 0)
        {
            return;
        }

        IsProcessing = true;
        previousBatchFinished = false;
        stopBatchRequested = false;
        skipCurrentRequested = false;
        batchCancellation = new CancellationTokenSource();
        batchCompletion = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        try
        {
            sleepInhibitor = SystemSleepInhibitor.Acquire();
        }
        catch (System.ComponentModel.Win32Exception)
        {
            sleepInhibitor = null;
            ReportStatus("无法临时阻止系统自动睡眠；请在本批处理期间保持电脑唤醒。", true);
        }

        UpdateTrayStatus("正在处理");
        UpdateButtons();
        queueList.Focus();

        var succeeded = 0;
        var failed = 0;
        var skipped = 0;
        var cancelled = 0;
        var processedCount = 0;

        try
        {
            foreach (var item in scopedItems)
            {
                if (batchCancellation.IsCancellationRequested)
                {
                    break;
                }

                if (item.Kind == MediaKind.Mp3)
                {
                    item.StepDetail = "按本次选择跳过现有 MP3";
                    item.ResultMessage = "未处理；原 MP3 未修改";
                    UpdateQueueRow(item);
                    skipped++;
                    processedCount++;
                    continue;
                }

                skipCurrentRequested = false;
                currentItemCancellation = CancellationTokenSource.CreateLinkedTokenSource(batchCancellation.Token);
                item.Stage = JobStage.Probing;
                item.StepDetail = "正在检查媒体格式、时长和第一条音轨";
                item.ResultMessage = string.Empty;
                UpdateQueueRow(item);
                progressBar.Value = 0;
                progressBar.AccessibleDescription = $"{item.FileName}，探测中，进度 0%。";
                ReportStatus($"第 {processedCount + 1} 项，共 {scopedItems.Length} 项：{item.FileName}，开始探测。", false);

                var progress = new Progress<int>(percentage =>
                {
                    item.Stage = JobStage.ConvertingMp3;
                    item.StepDetail = $"正在转换第一条音轨，{percentage}%";
                    progressBar.Value = Math.Clamp(percentage, 0, 100);
                    progressBar.AccessibleDescription = $"{item.FileName}，转换 MP3，{percentage}%。";
                    statusLabel.Text = $"状态：第 {processedCount + 1} 项，共 {scopedItems.Length} 项，正在转换 {item.FileName}，{percentage}%。";
                    statusLabel.AccessibleDescription = statusLabel.Text;
                    UpdateQueueRow(item, notifyAccessibility: false);
                });

                try
                {
                    await localVideoProcessor.ProcessAsync(
                        item,
                        settings.Mp3BitrateKbps,
                        progress,
                        currentItemCancellation.Token);
                    succeeded++;
                    progressBar.Value = 100;
                    progressBar.AccessibleDescription = $"{item.FileName}，转换完成，100%。";
                    UpdateQueueRow(item);
                    ReportStatus($"{item.FileName} 转换成功，MP3 已保存在源文件旁边。", false);
                }
                catch (OperationCanceledException)
                {
                    item.Stage = JobStage.Cancelled;
                    if (stopBatchRequested)
                    {
                        item.StepDetail = "已停止整个批次";
                        item.ResultMessage = "当前本地任务已取消；源文件未修改";
                    }
                    else if (skipCurrentRequested)
                    {
                        item.StepDetail = "已跳过当前项目，继续下一项";
                        item.ResultMessage = "当前项目已取消；源文件未修改";
                    }
                    else
                    {
                        item.StepDetail = "当前项目已取消";
                        item.ResultMessage = "源文件未修改";
                    }

                    cancelled++;
                    UpdateQueueRow(item);
                    ReportStatus($"{item.FileName} 已取消。", false);
                }
                catch (Exception exception)
                {
                    item.Stage = JobStage.Failed;
                    item.StepDetail = "处理失败";
                    item.ResultMessage = ToActionableError(exception);
                    failed++;
                    UpdateQueueRow(item);
                    ReportStatus($"{item.FileName} 失败：{item.ResultMessage} 将继续下一项。", true);
                }
                finally
                {
                    currentItemCancellation.Dispose();
                    currentItemCancellation = null;
                    processedCount++;
                }

                if (stopBatchRequested)
                {
                    break;
                }
            }
        }
        finally
        {
            var notStarted = scopedItems.Length - processedCount;
            IsProcessing = false;
            previousBatchFinished = true;
            currentItemCancellation?.Dispose();
            currentItemCancellation = null;
            batchCancellation.Dispose();
            batchCancellation = null;
            sleepInhibitor?.Dispose();
            sleepInhibitor = null;
            progressBar.Value = 0;
            progressBar.AccessibleDescription = "批次已结束，当前没有进行中的确定进度。";
            UpdateButtons();

            var summary = $"本批结束：成功 {succeeded} 个，失败 {failed} 个，跳过 {skipped} 个，已取消 {cancelled} 个，未开始 {notStarted} 个。成功的 MP3 保存在各自源文件旁边。";
            resultText.Text = summary + Environment.NewLine + Environment.NewLine +
                string.Join(Environment.NewLine, scopedItems.Select(item =>
                    $"{item.FileName}：{(string.IsNullOrWhiteSpace(item.ResultMessage) ? item.StepDetail : item.ResultMessage)}"));
            ReportStatus(summary, failed > 0);
            UpdateTrayStatus(failed > 0 ? "批次结束，有失败" : "批次完成");
            FocusBatchResult(scopedItems);
            batchCompletion?.TrySetResult();
        }
    }

    private async Task StartCloudBatchAsync(PasteDecision decision)
    {
        var credentials = await EnsureCloudCredentialsAsync();
        if (credentials is null)
        {
            ReportStatus("已取消腾讯云配置；没有上传文件或提交识别。", false);
            return;
        }

        var allItems = queueList.Items.Cast<ListViewItem>()
            .Select(row => row.Tag)
            .OfType<QueueItem>()
            .ToArray();
        var scopedItems = decision.FirstItemOnly ? allItems.Take(1).ToArray() : allItems;
        var cloudItems = scopedItems.Where(item =>
            item.Kind == MediaKind.Mp3 ? decision.TranscribeMp3 : decision.TranscribeVideos).ToArray();

        ReportStatus("正在探测本批媒体时长，用于提交前费用估算；尚未上传。", false);
        var estimatedDuration = TimeSpan.Zero;
        foreach (var item in cloudItems)
        {
            try
            {
                var probe = await mediaProbe.ProbeAsync(item.SourcePath, CancellationToken.None);
                if (probe.Duration > TimeSpan.Zero)
                {
                    estimatedDuration += probe.Duration;
                }
            }
            catch
            {
                // The item will report its actionable probe error during normal serial processing.
            }
        }

        var currentUsage = await usageLedger.GetCurrentMonthUsageAsync(credentials.SecretId, CancellationToken.None);
        var estimateText = $"本批将把临时音频上传到您自己的腾讯云私有 COS，并提交常规 16k_zh 录音文件识别。\r\n\r\n本批估算时长：{FormatDuration(estimatedDuration)}。\r\n本机本月已成功识别估算：{FormatDuration(currentUsage)}。\r\n提交后可能产生 ASR 和 COS 费用，且腾讯云任务不能取消。其他电脑、软件和控制台用量本机无法得知；腾讯云控制台是唯一权威数据。\r\n\r\n是否继续？";
        if (MessageBox.Show(
                this,
                estimateText,
                "确认上传腾讯云并可能产生费用",
                MessageBoxButtons.YesNo,
                MessageBoxIcon.Warning,
                MessageBoxDefaultButton.Button2) != DialogResult.Yes)
        {
            ReportStatus("已取消云端批次；没有上传文件或提交识别。", false);
            return;
        }

        if (currentUsage + estimatedDuration >= CloudLimits.MonthlyFreeAllowance)
        {
            var overLimitText = $"本机估算在本批后将达到 {FormatDuration(currentUsage + estimatedDuration)}，已达到或超过每月 10 小时保护线。实际费用和额度以腾讯云控制台为准。\r\n\r\n是否仍然提交？默认选择为取消。";
            if (MessageBox.Show(
                    this,
                    overLimitText,
                    "已达到本机 10 小时费用保护线",
                    MessageBoxButtons.YesNo,
                    MessageBoxIcon.Warning,
                    MessageBoxDefaultButton.Button2) != DialogResult.Yes)
            {
                ReportStatus("已由 10 小时费用保护取消提交；没有上传文件或提交识别。", true);
                return;
            }
        }

        var fallbackDirectory = ChooseMp3FallbackDirectoryIfNeeded(scopedItems, decision);
        if (fallbackDirectory.Cancelled)
        {
            ReportStatus("现有 MP3 的原目录不可写，且未选择备用目录；没有开始云端批次。", true);
            return;
        }

        IsProcessing = true;
        previousBatchFinished = false;
        stopBatchRequested = false;
        skipCurrentRequested = false;
        cloudTaskAlreadySubmitted = false;
        batchCancellation = new CancellationTokenSource();
        batchCompletion = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        try
        {
            sleepInhibitor = SystemSleepInhibitor.Acquire();
        }
        catch (System.ComponentModel.Win32Exception)
        {
            sleepInhibitor = null;
            ReportStatus("无法临时阻止系统自动睡眠；请在本批处理期间保持电脑唤醒。", true);
        }

        UpdateTrayStatus("正在处理云端批次");
        UpdateButtons();
        queueList.Focus();

        var succeeded = 0;
        var partiallySucceeded = 0;
        var failed = 0;
        var skipped = 0;
        var cancelled = 0;
        var recoverable = 0;
        var processedCount = 0;

        try
        {
            var cloudProcessor = await BuildCloudProcessorAsync(credentials, batchCancellation.Token);
            foreach (var item in scopedItems)
            {
                if (batchCancellation.IsCancellationRequested)
                {
                    break;
                }

                var shouldTranscribe = item.Kind == MediaKind.Mp3
                    ? decision.TranscribeMp3
                    : decision.TranscribeVideos;
                if (item.Kind == MediaKind.Mp3 && !shouldTranscribe)
                {
                    item.StepDetail = "按本次选择跳过现有 MP3";
                    item.ResultMessage = "未处理；原 MP3 未修改";
                    UpdateQueueRow(item);
                    skipped++;
                    processedCount++;
                    continue;
                }

                skipCurrentRequested = false;
                cloudTaskAlreadySubmitted = false;
                currentItemCancellation = CancellationTokenSource.CreateLinkedTokenSource(batchCancellation.Token);
                progressBar.Value = 0;
                var itemNumber = processedCount + 1;
                var progress = new Progress<CloudPhaseProgress>(update =>
                {
                    item.Stage = update.Stage;
                    item.StepDetail = update.Message + (update.Percentage is null ? string.Empty : $"，{update.Percentage}%");
                    if (update.Stage == JobStage.CloudTranscribing)
                    {
                        cloudTaskAlreadySubmitted = true;
                    }

                    if (update.Percentage is not null)
                    {
                        progressBar.Value = Math.Clamp(update.Percentage.Value, 0, 100);
                        progressBar.AccessibleDescription = $"{item.FileName}，{update.Message}，{update.Percentage}%。";
                    }
                    else
                    {
                        progressBar.Value = 0;
                        progressBar.AccessibleDescription = $"{item.FileName}，{update.Message}；该阶段没有真实百分比。";
                    }

                    statusLabel.Text = $"状态：第 {itemNumber} 项，共 {scopedItems.Length} 项，{item.FileName}，{item.StepDetail}。";
                    statusLabel.AccessibleDescription = statusLabel.Text;
                    UpdateQueueRow(item, notifyAccessibility: update.Percentage is null);
                });

                try
                {
                    CloudProcessingResult processingResult;
                    if (item.Kind == MediaKind.Mp3)
                    {
                        var sourceDirectory = Path.GetDirectoryName(item.SourcePath)
                            ?? throw new IOException("无法确定 MP3 所在目录。 ");
                        var outputDirectory = IsDirectoryWritable(sourceDirectory)
                            ? sourceDirectory
                            : fallbackDirectory.Path!;
                        processingResult = await cloudProcessor.ProcessMp3Async(
                            item,
                            outputDirectory,
                            progress,
                            currentItemCancellation.Token);
                    }
                    else if (shouldTranscribe)
                    {
                        processingResult = await cloudProcessor.ProcessVideoAsync(
                            item,
                            settings.Mp3BitrateKbps,
                            progress,
                            currentItemCancellation.Token);
                    }
                    else
                    {
                        await localVideoProcessor.ProcessAsync(
                            item,
                            settings.Mp3BitrateKbps,
                            new Progress<int>(percentage => ((IProgress<CloudPhaseProgress>)progress).Report(
                                new CloudPhaseProgress(JobStage.ConvertingMp3, "正在转换最终 MP3", percentage))),
                            currentItemCancellation.Token);
                        processingResult = new CloudProcessingResult(null, null, TimeSpan.Zero, false, false, null);
                    }

                    if (processingResult.ExceededDurationLimit || processingResult.CleanupWarning is not null)
                    {
                        partiallySucceeded++;
                    }
                    else
                    {
                        succeeded++;
                    }

                    UpdateQueueRow(item);
                    ReportStatus($"{item.FileName}：{item.ResultMessage}", processingResult.CleanupWarning is not null);
                }
                catch (CloudTaskContinuesException)
                {
                    item.Stage = JobStage.Recoverable;
                    item.StepDetail = "本地已停止跟踪；腾讯云任务仍可能继续";
                    item.ResultMessage = "任务已保存，重启软件后可恢复查询；仍可能占用额度";
                    recoverable++;
                    UpdateQueueRow(item);
                    ReportStatus($"{item.FileName} 已停止本地跟踪，但腾讯云任务无法取消，仍可能继续并占用额度。", true);
                }
                catch (OperationCanceledException)
                {
                    item.Stage = JobStage.Cancelled;
                    item.StepDetail = stopBatchRequested ? "已停止整个批次" : "已跳过当前项目";
                    item.ResultMessage = "尚未提交的本地或上传步骤已取消；源文件未修改";
                    cancelled++;
                    UpdateQueueRow(item);
                }
                catch (Exception exception)
                {
                    var mp3WasGenerated = item.Kind != MediaKind.Mp3 &&
                        item.ResultMessage.StartsWith("成功生成 ", StringComparison.Ordinal);
                    if (mp3WasGenerated)
                    {
                        item.Stage = JobStage.PartiallySucceeded;
                        item.StepDetail = "MP3 已成功；TXT 处理失败";
                        item.ResultMessage = $"{item.ResultMessage}；TXT：{ToActionableError(exception)}";
                        partiallySucceeded++;
                    }
                    else
                    {
                        item.Stage = JobStage.Failed;
                        item.StepDetail = "处理失败";
                        item.ResultMessage = ToActionableError(exception);
                        failed++;
                    }

                    UpdateQueueRow(item);
                    ReportStatus($"{item.FileName}：{item.ResultMessage} 将继续下一项。", true);
                }
                finally
                {
                    currentItemCancellation.Dispose();
                    currentItemCancellation = null;
                    cloudTaskAlreadySubmitted = false;
                    processedCount++;
                }

                if (stopBatchRequested)
                {
                    break;
                }
            }
        }
        catch (OperationCanceledException)
        {
            // Cancellation before the first item or while provisioning is summarized below.
        }
        catch (Exception exception)
        {
            failed++;
            ReportStatus($"腾讯云批次初始化失败：{ToActionableError(exception)}", true);
        }
        finally
        {
            var notStarted = scopedItems.Length - processedCount;
            IsProcessing = false;
            previousBatchFinished = true;
            cloudTaskAlreadySubmitted = false;
            currentItemCancellation?.Dispose();
            currentItemCancellation = null;
            batchCancellation.Dispose();
            batchCancellation = null;
            sleepInhibitor?.Dispose();
            sleepInhibitor = null;
            progressBar.Value = 0;
            progressBar.AccessibleDescription = "批次已结束，当前没有进行中的确定进度。";
            UpdateButtons();

            var summary = $"本批结束：成功 {succeeded} 个，部分成功 {partiallySucceeded} 个，失败 {failed} 个，跳过 {skipped} 个，已取消 {cancelled} 个，待恢复 {recoverable} 个，未开始 {notStarted} 个。";
            resultText.Text = summary + Environment.NewLine + Environment.NewLine +
                string.Join(Environment.NewLine, scopedItems.Select(item =>
                    $"{item.FileName}：{(string.IsNullOrWhiteSpace(item.ResultMessage) ? item.StepDetail : item.ResultMessage)}"));
            ReportStatus(summary, failed > 0 || recoverable > 0);
            UpdateTrayStatus(failed > 0 || recoverable > 0 ? "批次结束，需要检查" : "批次完成");
            FocusBatchResult(scopedItems);
            batchCompletion?.TrySetResult();
        }
    }

    private async Task<CloudJobProcessor> BuildCloudProcessorAsync(
        CloudCredentials credentials,
        CancellationToken cancellationToken,
        string? recoveryBucket = null)
    {
        var objectStore = new TencentCosObjectStore(credentials, recoveryBucket ?? settings.CosBucket);
        var bucket = await objectStore.EnsureReadyAsync(cancellationToken);
        if (!string.Equals(settings.CosBucket, bucket, StringComparison.Ordinal))
        {
            settings = settings with { CosBucket = bucket, CosRegion = TencentCosPolicy.Region };
            await settingsStore.SaveAsync(settings, cancellationToken);
        }

        return new CloudJobProcessor(
            mediaProbe,
            audioConverter,
            localVideoProcessor,
            outputCommitter,
            objectStore,
            new TencentCloudTranscriber(credentials),
            jobStore,
            usageLedger,
            localDataPaths,
            credentials);
    }

    private async Task<CloudCredentials?> EnsureCloudCredentialsAsync()
    {
        try
        {
            var stored = await settingsStore.LoadCredentialsAsync(CancellationToken.None);
            if (stored is not null)
            {
                return stored;
            }
        }
        catch (Exception exception) when (exception is System.Security.Cryptography.CryptographicException or IOException)
        {
            MessageBox.Show(
                this,
                "已保存的腾讯云凭据无法由当前 Windows 用户解密。请重新配置；旧文件没有写入日志。",
                "腾讯云凭据不可用",
                MessageBoxButtons.OK,
                MessageBoxIcon.Warning);
        }

        return await ConfigureCloudCredentialsAsync();
    }

    private async Task<CloudCredentials?> ConfigureCloudCredentialsAsync()
    {
        using var dialog = new CloudCredentialsDialog();
        if (dialog.ShowDialog(this) != DialogResult.OK)
        {
            return null;
        }

        try
        {
            TencentCosPolicy.ValidateAppId(dialog.Credentials.AppId);
            await settingsStore.SaveCredentialsAsync(dialog.Credentials, CancellationToken.None);
            settings = settings with { CosBucket = null, CosRegion = TencentCosPolicy.Region };
            await settingsStore.SaveAsync(settings, CancellationToken.None);
            ReportStatus("腾讯云配置已用 DPAPI 为当前 Windows 用户加密保存；未上传测试音频。", false);
            return dialog.Credentials;
        }
        catch (ArgumentException exception)
        {
            MessageBox.Show(this, exception.Message, "腾讯云配置无效", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return null;
        }
    }

    private (bool Cancelled, string? Path) ChooseMp3FallbackDirectoryIfNeeded(
        IReadOnlyCollection<QueueItem> items,
        PasteDecision decision)
    {
        if (!decision.TranscribeMp3)
        {
            return (false, null);
        }

        var needsFallback = items
            .Where(item => item.Kind == MediaKind.Mp3)
            .Select(item => Path.GetDirectoryName(item.SourcePath))
            .Where(directory => directory is not null)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .Any(directory => !IsDirectoryWritable(directory!));
        if (!needsFallback)
        {
            return (false, null);
        }

        using var dialog = new FolderBrowserDialog
        {
            Description = "本批至少一个 MP3 的原目录不可写。请选择一次备用 TXT 保存目录。",
            UseDescriptionForTitle = true,
            ShowNewFolderButton = true
        };
        return dialog.ShowDialog(this) == DialogResult.OK
            ? (false, dialog.SelectedPath)
            : (true, null);
    }

    private static bool IsDirectoryWritable(string directory)
    {
        var probePath = Path.Combine(directory, $".accessible-video-to-text.{Guid.NewGuid():N}.write-test");
        try
        {
            using (new FileStream(probePath, FileMode.CreateNew, FileAccess.Write, FileShare.None, 1, FileOptions.DeleteOnClose))
            {
            }

            return true;
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or DirectoryNotFoundException)
        {
            return false;
        }
        finally
        {
            try
            {
                if (File.Exists(probePath))
                {
                    File.Delete(probePath);
                }
            }
            catch
            {
                // A zero-byte probe file is app-owned and can be removed by the user if a provider ignored DeleteOnClose.
            }
        }
    }

    private static string FormatDuration(TimeSpan duration) =>
        duration.TotalHours >= 1
            ? $"{(int)duration.TotalHours} 小时 {duration.Minutes} 分钟"
            : $"{duration.Minutes} 分 {duration.Seconds} 秒";

    private async Task PromptForPendingRecoveryAsync()
    {
        IReadOnlyList<CloudRecoveryJob> jobs;
        try
        {
            jobs = await jobStore.LoadPendingAsync(CancellationToken.None);
        }
        catch (Exception exception) when (exception is IOException or System.Text.Json.JsonException)
        {
            ReportStatus("待恢复任务文件无法读取，未自动覆盖；请先保留本机数据并检查文件。", true);
            return;
        }

        if (jobs.Count == 0)
        {
            return;
        }

        var choice = MessageBox.Show(
            this,
            $"发现 {jobs.Count} 个待恢复腾讯云任务。已提交任务不能取消，仍可能继续并占用额度。\r\n\r\n是：全部继续查询（默认）\r\n否：暂不处理\r\n取消：查看详情",
            "待恢复腾讯云任务",
            MessageBoxButtons.YesNoCancel,
            MessageBoxIcon.Warning,
            MessageBoxDefaultButton.Button1);
        if (choice == DialogResult.Cancel)
        {
            var details = string.Join(Environment.NewLine, jobs.Select((job, index) =>
                $"{index + 1}. TXT：{Path.GetFileName(job.OutputPath)}；阶段：{job.Stage.ToAccessibleText()}；提交：{job.SubmittedAt.ToLocalTime():yyyy-MM-dd HH:mm:ss}"));
            MessageBox.Show(this, details, "待恢复任务详情", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }

        if (choice != DialogResult.Yes)
        {
            ReportStatus($"已暂不处理 {jobs.Count} 个待恢复任务；下次启动会再次询问。", true);
            return;
        }

        var credentials = await EnsureCloudCredentialsAsync();
        if (credentials is null)
        {
            ReportStatus("没有可用腾讯云凭据，待恢复任务保持不变。", true);
            return;
        }

        IsProcessing = true;
        stopBatchRequested = false;
        cloudTaskAlreadySubmitted = true;
        batchCancellation = new CancellationTokenSource();
        batchCompletion = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        UpdateButtons();
        UpdateTrayStatus("正在恢复云端任务");
        try
        {
            sleepInhibitor = SystemSleepInhibitor.Acquire();
            var processor = await BuildCloudProcessorAsync(credentials, batchCancellation.Token, jobs[0].Bucket);
            var succeeded = 0;
            var failed = 0;
            foreach (var job in jobs)
            {
                currentItemCancellation = CancellationTokenSource.CreateLinkedTokenSource(batchCancellation.Token);
                try
                {
                    var progress = new Progress<CloudPhaseProgress>(update =>
                    {
                        statusLabel.Text = $"状态：恢复任务 {Path.GetFileName(job.OutputPath)}，{update.Message}。";
                        statusLabel.AccessibleDescription = statusLabel.Text;
                        progressBar.Value = update.Percentage is null ? 0 : Math.Clamp(update.Percentage.Value, 0, 100);
                    });
                    await processor.ResumeAsync(job, progress, currentItemCancellation.Token);
                    succeeded++;
                }
                catch (CloudTaskContinuesException)
                {
                    ReportStatus("已停止本地恢复查询；腾讯云任务仍可能继续并占用额度，恢复记录已保留。", true);
                }
                catch (OperationCanceledException)
                {
                    ReportStatus("已停止恢复查询；未结束的腾讯云任务记录仍保留。", true);
                }
                catch (Exception exception)
                {
                    failed++;
                    ReportStatus($"恢复 {Path.GetFileName(job.OutputPath)} 失败：{ToActionableError(exception)}", true);
                }
                finally
                {
                    currentItemCancellation.Dispose();
                    currentItemCancellation = null;
                }

                if (stopBatchRequested)
                {
                    break;
                }
            }

            resultText.Text = $"待恢复任务处理结束：成功 {succeeded} 个，失败 {failed} 个；未结束记录会在下次启动继续提示。";
        }
        catch (Exception exception)
        {
            ReportStatus($"无法开始恢复腾讯云任务：{ToActionableError(exception)}", true);
        }
        finally
        {
            IsProcessing = false;
            cloudTaskAlreadySubmitted = false;
            batchCancellation.Dispose();
            batchCancellation = null;
            sleepInhibitor?.Dispose();
            sleepInhibitor = null;
            progressBar.Value = 0;
            UpdateButtons();
            UpdateTrayStatus("空闲");
            batchCompletion?.TrySetResult();
            queueList.Focus();
        }
    }

    private void ConfigureTrayIcon()
    {
        var trayMenu = new ContextMenuStrip();
        var showItem = new ToolStripMenuItem("显示主窗口", null, (_, _) => ShowMainWindow());
        var exitItem = new ToolStripMenuItem("退出软件", null, async (_, _) => await ExitFromTrayAsync());
        trayMenu.Items.AddRange([showItem, exitItem]);

        trayIcon.Icon = SystemIcons.Application;
        trayIcon.Text = "无障碍视频转文字：空闲";
        trayIcon.Visible = false;
        trayIcon.ContextMenuStrip = trayMenu;
        trayIcon.MouseClick += (_, eventArgs) =>
        {
            if (eventArgs.Button == MouseButtons.Left)
            {
                ShowMainWindow();
            }
        };
        trayIcon.DoubleClick += (_, _) => ShowMainWindow();
    }

    private void HandleFormClosing(object? sender, FormClosingEventArgs eventArgs)
    {
        if (IsProcessing && !exitRequested)
        {
            eventArgs.Cancel = true;
            HideToTray();
            return;
        }

        trayIcon.Visible = false;
    }

    private void HideToTray()
    {
        trayIcon.Visible = true;
        UpdateTrayStatus(IsProcessing ? "正在处理" : "空闲");
        Hide();
    }

    private void ShowMainWindow()
    {
        Show();
        WindowState = FormWindowState.Normal;
        Activate();
        queueList.Focus();
    }

    private async Task ExitFromTrayAsync()
    {
        if (IsProcessing)
        {
            using var dialog = new TrayExitDialog();
            if (dialog.ShowDialog() != DialogResult.OK)
            {
                UpdateTrayStatus("正在处理");
                return;
            }

            stopBatchRequested = true;
            batchCancellation?.Cancel();
            if (batchCompletion is not null)
            {
                await batchCompletion.Task;
            }
        }

        exitRequested = true;
        trayIcon.Visible = false;
        Close();
    }

    private void UpdateTrayStatus(string status)
    {
        var text = $"无障碍视频转文字：{status}";
        trayIcon.Text = text.Length <= 63 ? text : text[..63];
    }

    private void ShowCancellationScopeDialog()
    {
        if (!IsProcessing)
        {
            return;
        }

        using var dialog = new CancellationScopeDialog(cloudTaskAlreadySubmitted);
        dialog.ShowDialog(this);
        switch (dialog.Choice)
        {
            case CancellationScope.SkipCurrent:
                skipCurrentRequested = true;
                currentItemCancellation?.Cancel();
                ReportStatus("正在跳过当前项目；清理临时文件后会继续下一项。", false);
                break;
            case CancellationScope.StopBatch:
                stopBatchRequested = true;
                batchCancellation?.Cancel();
                ReportStatus("正在停止整个批次；已开始的临时输出会清理。", false);
                break;
            default:
                ReportStatus("继续处理当前批次。", false);
                queueList.Focus();
                break;
        }
    }

    private void UpdateQueueRow(QueueItem item, bool notifyAccessibility = true)
    {
        var row = queueList.Items.Cast<ListViewItem>()
            .FirstOrDefault(candidate => candidate.Tag is QueueItem queued && queued.Id == item.Id);
        if (row is null)
        {
            return;
        }

        row.Text = item.FileName;
        row.SubItems[1].Text = item.KindText;
        row.SubItems[2].Text = item.Stage.ToAccessibleText();
        row.SubItems[3].Text = string.IsNullOrWhiteSpace(item.ResultMessage)
            ? item.StepDetail
            : $"{item.StepDetail}；{item.ResultMessage}";
        row.ToolTipText = $"{item.FileName}，{item.KindText}，{item.Stage.ToAccessibleText()}，{row.SubItems[3].Text}";
        if (notifyAccessibility)
        {
            AccessibilityNotifyClients(AccessibleEvents.NameChange, row.Index + 1);
        }
    }

    private void FocusBatchResult(IReadOnlyCollection<QueueItem> scopedItems)
    {
        var failedItem = scopedItems.FirstOrDefault(item => item.Stage == JobStage.Failed);
        var target = failedItem ?? scopedItems.FirstOrDefault();
        if (target is null)
        {
            queueList.Focus();
            return;
        }

        var row = queueList.Items.Cast<ListViewItem>()
            .FirstOrDefault(candidate => candidate.Tag is QueueItem queued && queued.Id == target.Id);
        if (row is not null)
        {
            queueList.SelectedItems.Clear();
            row.Selected = true;
            row.Focused = true;
            row.EnsureVisible();
        }

        queueList.Focus();
    }

    private static string ToActionableError(Exception exception) => exception switch
    {
        FileNotFoundException => "源文件已经不存在。请重新复制该文件后再试。",
        UnauthorizedAccessException => "没有读取源文件或写入源目录的权限。请检查权限后重试。",
        TencentCloud.Common.TencentCloudSDKException cloud when
            (cloud.ErrorCode ?? string.Empty).Contains("AuthFailure", StringComparison.OrdinalIgnoreCase) =>
            "腾讯云认证失败。请在设置中重新填写新建的 SecretId 和 SecretKey，并确认旧密钥已撤销。",
        TencentCloud.Common.TencentCloudSDKException cloud when
            (cloud.ErrorCode ?? string.Empty).Contains("UnauthorizedOperation", StringComparison.OrdinalIgnoreCase) =>
            "腾讯云权限不足。请检查最小权限 CAM 子账号是否具有本应用所需的 ASR 和专用 COS 权限。",
        TencentCloud.Common.TencentCloudSDKException cloud when
            (cloud.ErrorCode ?? string.Empty).Contains("LimitExceeded", StringComparison.OrdinalIgnoreCase) =>
            "腾讯云额度或调用限制已达到。请到腾讯云控制台核对额度和费用状态。",
        COSXML.CosException.CosServerException cos when cos.statusCode is 401 or 403 =>
            "腾讯云 COS 认证或权限不足。请检查 AppID、凭据和专用桶权限。",
        COSXML.CosException.CosServerException cos when cos.statusCode == 409 =>
            "腾讯云 COS 存储桶名称冲突或资源状态冲突；软件未操作其他存储桶。请重试。",
        HttpRequestException or TimeoutException => "网络暂时不可用，已按 2、5、15 秒重试。请检查系统网络和代理设置后再试。",
        InvalidDataException => exception.Message.Trim(),
        IOException when exception.Message.Contains("space", StringComparison.OrdinalIgnoreCase) => "磁盘空间不足。请释放空间后重试。",
        IOException => "媒体转换失败或输出不可写。请检查文件是否损坏、磁盘空间和目录权限。",
        _ when exception.GetType().Name.Contains("FFMpeg", StringComparison.OrdinalIgnoreCase) => "FFmpeg 无法解码该文件或转换失败。请检查文件是否损坏以及编解码器支持。",
        _ => "发生未预期错误。为保护隐私，界面不显示完整路径或底层命令。"
    };

    private void ReportStatus(string message, bool actionRequired)
    {
        statusLabel.Text = $"状态：{message}";
        statusLabel.AccessibleDescription = message;
        AccessibilityNotifyClients(AccessibleEvents.NameChange, -1);
    }

    private async void ShowSettingsPlaceholder()
    {
        if (IsProcessing)
        {
            ReportStatus("正在处理任务，暂时不能修改设置或清除本机数据。", true);
            return;
        }

        var credentialsConfigured = false;
        try
        {
            credentialsConfigured = await settingsStore.LoadCredentialsAsync(CancellationToken.None) is not null;
        }
        catch (Exception exception) when (exception is IOException or System.Security.Cryptography.CryptographicException)
        {
            credentialsConfigured = false;
        }

        using var dialog = new SettingsDialog(settings.Mp3BitrateKbps, credentialsConfigured);
        if (dialog.ShowDialog(this) == DialogResult.OK)
        {
            switch (dialog.Action)
            {
                case SettingsDialogAction.ConfigureCloud:
                    if ((await jobStore.LoadPendingAsync(CancellationToken.None)).Count > 0)
                    {
                        MessageBox.Show(
                            this,
                            "存在待恢复腾讯云任务。为避免更换凭据后无法查询或清理，现在禁止重新配置；请先恢复或结束这些任务。",
                            "不能更换腾讯云凭据",
                            MessageBoxButtons.OK,
                            MessageBoxIcon.Warning);
                    }
                    else
                    {
                        await ConfigureCloudCredentialsAsync();
                    }
                    break;
                case SettingsDialogAction.ClearLocalData:
                    await ClearLocalDataAsync();
                    break;
                default:
                    settings = settings with { Mp3BitrateKbps = dialog.Mp3BitrateKbps };
                    await settingsStore.SaveAsync(settings, CancellationToken.None);
                    ReportStatus($"设置已保存。最终 MP3 码率为 {settings.Mp3BitrateKbps} kbps。", false);
                    break;
            }
        }

        queueList.Focus();
    }

    private async Task ClearLocalDataAsync()
    {
        var pending = await jobStore.LoadPendingAsync(CancellationToken.None);
        if (pending.Count > 0)
        {
            MessageBox.Show(
                this,
                $"存在 {pending.Count} 个待恢复腾讯云任务。为避免丢失查询和清理信息，现在禁止清除。请先继续查询或明确结束恢复状态。",
                "不能清除本机数据",
                MessageBoxButtons.OK,
                MessageBoxIcon.Warning);
            return;
        }

        if (MessageBox.Show(
                this,
                "这会删除本机设置、DPAPI 加密凭据、用量估算、运行日志和任务记录。不会删除源媒体或已经生成的 MP3/TXT。是否继续？",
                "第一次确认清除本机全部数据",
                MessageBoxButtons.YesNo,
                MessageBoxIcon.Warning,
                MessageBoxDefaultButton.Button2) != DialogResult.Yes)
        {
            return;
        }

        if (MessageBox.Show(
                this,
                "请再次确认：清除后腾讯云凭据无法恢复，下一次使用云端功能必须重新输入。确定清除吗？",
                "第二次确认清除本机全部数据",
                MessageBoxButtons.YesNo,
                MessageBoxIcon.Warning,
                MessageBoxDefaultButton.Button2) != DialogResult.Yes)
        {
            return;
        }

        var expectedRoot = Path.GetFullPath(Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "AccessibleVideoToText"));
        if (!Path.GetFullPath(localDataPaths.BaseDirectory).Equals(expectedRoot, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException("本机数据目录不符合安全边界，已拒绝清除。 ");
        }

        foreach (var file in new[]
                 {
                     localDataPaths.SettingsFile,
                     localDataPaths.CredentialsFile,
                     localDataPaths.JobsFile,
                     localDataPaths.UsageLedgerFile
                 })
        {
            if (File.Exists(file))
            {
                File.Delete(file);
            }
        }

        foreach (var directory in new[] { localDataPaths.LogsDirectory, localDataPaths.TempDirectory })
        {
            if (Directory.Exists(directory))
            {
                Directory.Delete(directory, recursive: true);
            }
        }

        localDataPaths.EnsureDirectories();
        settings = new AppSettings();
        ReportStatus("本机设置、加密凭据、用量估算、日志和任务记录已清除；源媒体和输出文件未删除。", false);
    }

    private void ShowOutputPlaceholder()
    {
        MessageBox.Show(this, "结果默认保存在源文件旁边。软件不会自动打开文件或文件夹。", "结果目录", MessageBoxButtons.OK, MessageBoxIcon.Information);
        queueList.Focus();
    }

    private void ShowHelp()
    {
        const string help = "Ctrl+V：粘贴资源管理器文件。\r\nCtrl+O：添加文件。\r\nDelete：只从队列移除。\r\nAlt+S：设置。\r\nAlt+O：结果目录。\r\nEsc：任务中选择取消范围。\r\n\r\n云端转文字会把临时音频上传到您自己的腾讯云私有 COS，并可能产生 ASR 和 COS 费用；真正提交前会再次确认。";
        MessageBox.Show(this, help, "键盘和云端帮助", MessageBoxButtons.OK, MessageBoxIcon.Information);
        queueList.Focus();
    }
}
