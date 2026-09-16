using System.Diagnostics;
using AccessibleVideoToText.Core;
using AccessibleVideoToText.Infrastructure;
using System.Windows.Forms.Automation;

namespace AccessibleVideoToText.App;

public sealed class MainForm : Form
{
    private readonly ClipboardBatchAnalyzer clipboardAnalyzer = new();
    private readonly TaskCompletionNotifier completionNotifier = new(CompletionSound.Play);
    private readonly LocalDataPaths localDataPaths;
    private readonly DpapiSettingsStore settingsStore;
    private readonly IMediaProbe mediaProbe;
    private readonly IAudioConverter audioConverter;
    private readonly IAtomicOutputCommitter outputCommitter;
    private readonly JsonJobStore jobStore;
    private readonly JsonUsageLedger usageLedger;
    private readonly LocalVideoProcessor localVideoProcessor;
    private readonly WeChatXiaoeCourseAutomation xiaoeCourseAutomation = new();
    private readonly TextBox linkInput = new();
    private readonly ComboBox processingMode = new();
    private readonly ComboBox douyinNoteContent = new();
    private readonly TabControl taskTabs = new();
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
    private readonly ToolStripMenuItem qualityBestItem = new("最高品质（默认）");
    private readonly ToolStripMenuItem qualityAskItem = new("每次询问品质");
    private readonly ToolStripMenuItem douyinLoginItem = new("抖音专用登录") { CheckOnClick = true };
    private CancellationTokenSource? batchCancellation;
    private CancellationTokenSource? currentItemCancellation;
    private TaskCompletionSource? batchCompletion;
    private SystemSleepInhibitor? sleepInhibitor;
    private FeichuanWorkerClient? workerClient;
    private string? currentResultDirectory;
    private AppSettings settings = new();
    private bool skipCurrentRequested;
    private bool stopBatchRequested;
    private bool exitRequested;
    private bool cloudTaskAlreadySubmitted;
    private bool isProcessing;
    private bool IsProcessing
    {
        get => isProcessing;
        set
        {
            isProcessing = value;
            UpdateProgressWindowTitle();
        }
    }
    private bool previousBatchFinished;
    private string currentTaskActivity = "当前没有进行中的任务";
    private int? currentTaskPercentage;
    private string? lastProgressAnnouncement;
    private int progressAnnouncementRequestCount;

    public MainForm()
    {
        Text = "飞船下载转换工具";
        AccessibleName = "飞船下载转换工具主窗口";
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

        openFileDialog.Title = "选择视频或音频";
        openFileDialog.Multiselect = true;
        openFileDialog.CheckFileExists = true;
        openFileDialog.Filter = "视频和音频|*.mp4;*.mkv;*.mov;*.avi;*.wmv;*.flv;*.webm;*.m4v;*.mpeg;*.mpg;*.ts;*.m2ts;*.3gp;*.mp3;*.m4a;*.aac;*.flac;*.wav;*.ogg;*.opus;*.wma|所有文件|*.*";

        ConfigureTrayIcon();
        FormClosing += HandleFormClosing;
        FormClosed += async (_, _) =>
        {
            trayIcon.Visible = false;
            trayIcon.Dispose();
            sleepInhibitor?.Dispose();
            if (workerClient is not null)
            {
                await workerClient.DisposeAsync();
                workerClient = null;
            }
        };

        Shown += async (_, _) =>
        {
            await LoadSettingsAsync();
            await LoadWorkerPreferencesAsync();
            await ShowFirstRunHelpIfNeededAsync();
            await PromptForPendingRecoveryAsync();
            linkInput.Focus();
            linkInput.SelectAll();
        };
    }

    protected override bool ProcessCmdKey(ref Message msg, Keys keyData)
    {
        if (IsProcessing && IsPhysicalNumpad8(msg, keyData))
        {
            AnnounceCurrentTaskProgress();
            return true;
        }

        if (keyData == (Keys.Control | Keys.V))
        {
            PasteClipboardContent();
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

    private static bool IsPhysicalNumpad8(Message message, Keys keyData)
    {
        if ((keyData & Keys.Modifiers) != Keys.None)
        {
            return false;
        }

        var keyCode = keyData & Keys.KeyCode;
        if (keyCode == Keys.NumPad8)
        {
            return true;
        }

        // Num Lock 关闭时，物理小键盘 8 会作为非扩展的 VK_UP 到达。
        // 独立方向键上是扩展键，不能被本快捷键误拦截。
        var keyBits = message.LParam.ToInt64();
        var scanCode = (keyBits >> 16) & 0xff;
        var isExtended = ((keyBits >> 24) & 1) != 0;
        return keyCode == Keys.Up && scanCode == 0x48 && !isExtended;
    }

    private void AnnounceCurrentTaskProgress()
    {
        var activity = string.IsNullOrWhiteSpace(currentTaskActivity)
            ? "当前任务正在处理"
            : currentTaskActivity.Trim().TrimEnd('。', '！', '；');
        var announcement = currentTaskPercentage is int percentage
            ? $"{activity}，进度 {Math.Clamp(percentage, 0, 100)}%。"
            : $"{activity}，暂时没有可用百分比。";

        lastProgressAnnouncement = announcement;
        progressAnnouncementRequestCount++;
        var raised = statusLabel.AccessibilityObject.RaiseAutomationNotification(
            AutomationNotificationKind.Other,
            AutomationNotificationProcessing.ImportantMostRecent,
            announcement);
        if (!raised)
        {
            statusLabel.AccessibleDescription = announcement;
            AccessibilityNotifyClients(AccessibleEvents.DescriptionChange, -1);
        }
    }

    private void SetCurrentTaskProgress(string activity, int? percentage)
    {
        if (!string.IsNullOrWhiteSpace(activity))
        {
            currentTaskActivity = activity.Trim();
        }

        currentTaskPercentage = percentage is null ? null : Math.Clamp(percentage.Value, 0, 100);
        UpdateProgressWindowTitle();
    }

    private void UpdateProgressWindowTitle()
    {
        const string productName = "飞船下载转换工具";
        if (!IsProcessing)
        {
            Text = productName;
            AccessibleName = productName + "主窗口";
            return;
        }

        // 争渡会先截获小键盘 8 并朗读当前窗口，应用收不到该按键。
        // 因此把最近的确定进度同步到窗口标题，让它朗读的当前窗口信息
        // 本身就包含百分比；未截获按键的读屏软件仍走 UIA 通知路径。
        var percentage = currentTaskPercentage ?? (progressBar.Value > 0 ? progressBar.Value : null);
        var progressText = percentage is int value
            ? $"当前任务进度 {Math.Clamp(value, 0, 100)}%"
            : "当前任务正在处理，暂时没有可用百分比";
        Text = $"{productName}，{progressText}";
        AccessibleName = Text + "，主窗口";
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
        var pasteItem = new ToolStripMenuItem("粘贴链接或资源管理器文件(&V)", null, (_, _) => PasteClipboardContent())
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
        var downloadFolderItem = new ToolStripMenuItem(
            "下载文件夹(&D)",
            null,
            async (_, _) => await ChooseDownloadFolderAsync());
        var xiaoeCourseItem = new ToolStripMenuItem(
            "下载微信当前小鹅通已购课程(&W)",
            null,
            async (_, _) => await StartCurrentWechatXiaoeCourseAsync())
        {
            ShortcutKeys = Keys.Alt | Keys.W,
            ToolTipText = "只枚举微信当前课程目录中的直播回放；图文和推荐内容不会下载。"
        };
        var qualityMenu = new ToolStripMenuItem("品质选择(&Q)");
        qualityBestItem.Checked = true;
        qualityBestItem.Click += async (_, _) => await SetQualityModeAsync(askEachTime: false);
        qualityAskItem.Click += async (_, _) => await SetQualityModeAsync(askEachTime: true);
        qualityMenu.DropDownItems.AddRange([qualityBestItem, qualityAskItem]);
        douyinLoginItem.ToolTipText = "需要时用软件独立浏览器资料登录抖音；不读取日常浏览器 Cookie。";
        var clearDouyinLogin = new ToolStripMenuItem(
            "清除抖音专用登录(&C)",
            null,
            async (_, _) => await ClearDouyinLoginAsync());
        actionMenu.DropDownItems.AddRange([
            pasteItem,
            downloadFolderItem,
            xiaoeCourseItem,
            qualityMenu,
            douyinLoginItem,
            clearDouyinLogin,
            new ToolStripSeparator(),
            settingsItem,
            outputItem
        ]);

        var updateMenu = new ToolStripMenuItem("更新(&U)");
        var softwareUpdate = new ToolStripMenuItem(
            "手动检查软件更新(&S)",
            null,
            async (_, _) => await CheckSoftwareUpdateAsync());
        var coreUpdate = new ToolStripMenuItem(
            "手动检查下载核心更新(&C)",
            null,
            async (_, _) => await CheckCoreUpdateAsync());
        updateMenu.DropDownItems.AddRange([softwareUpdate, coreUpdate]);

        var helpMenu = new ToolStripMenuItem("帮助(&H)");
        var keyboardHelp = new ToolStripMenuItem("键盘和云端帮助(&H)", null, (_, _) => ShowHelp())
        {
            ShortcutKeys = Keys.F1
        };
        var donation = new ToolStripMenuItem("打赏(&D)", null, (_, _) => ShowDonation());
        helpMenu.DropDownItems.AddRange([keyboardHelp, donation]);

        menu.Items.AddRange([fileMenu, actionMenu, updateMenu, helpMenu]);
        return menu;
    }

    private Control BuildLayout()
    {
        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            Padding = new Padding(12),
            ColumnCount = 1,
            RowCount = 9
        };
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));

        var linkLabel = new Label
        {
            Text = "下载链接或平台分享文本(&L)",
            AutoSize = true,
            AccessibleName = "下载链接或平台分享文本标签"
        };

        linkInput.Dock = DockStyle.Top;
        linkInput.AccessibleName = "下载链接或平台分享文本";
        linkInput.AccessibleDescription = "粘贴普通下载链接或平台完整分享文本。在此处按 Enter 开始。";
        linkInput.PlaceholderText = "请粘贴下载链接或平台分享文本";
        linkInput.TabIndex = 0;
        linkInput.TextChanged += (_, _) => UpdateButtons();
        linkInput.KeyDown += (_, eventArgs) =>
        {
            if (eventArgs.KeyCode == Keys.Enter)
            {
                eventArgs.SuppressKeyPress = true;
                StartCurrentInput();
            }
        };
        linkLabel.TabIndex = 0;

        var modePanel = new FlowLayoutPanel
        {
            AutoSize = true,
            Dock = DockStyle.Fill,
            FlowDirection = FlowDirection.LeftToRight,
            WrapContents = true,
            Margin = new Padding(0, 8, 0, 8),
            TabIndex = 1
        };
        var modeLabel = new Label
        {
            Text = "处理模式(&M)",
            AutoSize = true,
            AccessibleName = "处理模式标签",
            Margin = new Padding(0, 6, 8, 0)
        };
        processingMode.DropDownStyle = ComboBoxStyle.DropDownList;
        processingMode.AccessibleName = "处理模式";
        processingMode.AccessibleDescription = "上下方向键选择下载、转换或获取解析直连，在此处按 Enter 开始。";
        processingMode.Items.AddRange([
            "只下载",
            "下载后转换为 MP3",
            "下载、转换 MP3 并生成 TXT",
            "获取解析直连"
        ]);
        processingMode.SelectedIndex = 0;
        processingMode.TabIndex = 1;
        processingMode.Width = 300;
        processingMode.KeyDown += (_, eventArgs) =>
        {
            if (eventArgs.KeyCode == Keys.Enter)
            {
                eventArgs.SuppressKeyPress = true;
                StartCurrentInput();
            }
        };
        modePanel.Controls.Add(modeLabel);
        modePanel.Controls.Add(processingMode);

        var noteContentPanel = new FlowLayoutPanel
        {
            AutoSize = true,
            Dock = DockStyle.Fill,
            FlowDirection = FlowDirection.LeftToRight,
            WrapContents = true,
            Margin = new Padding(0, 0, 0, 8),
            TabIndex = 2
        };
        var noteContentLabel = new Label
        {
            Text = "图文下载内容(&I)",
            AutoSize = true,
            AccessibleName = "图文下载内容标签",
            Margin = new Padding(0, 6, 8, 0)
        };
        douyinNoteContent.DropDownStyle = ComboBoxStyle.DropDownList;
        douyinNoteContent.AccessibleName = "图文下载内容";
        douyinNoteContent.AccessibleDescription = "只对抖音图文作品生效；选择仅下载音频或下载图片和音频，在此处按 Enter 开始。";
        douyinNoteContent.Items.AddRange([
            "仅下载音频",
            "下载图片和音频"
        ]);
        douyinNoteContent.SelectedIndex = 0;
        douyinNoteContent.TabIndex = 2;
        douyinNoteContent.Width = 300;
        douyinNoteContent.KeyDown += (_, eventArgs) =>
        {
            if (eventArgs.KeyCode == Keys.Enter)
            {
                eventArgs.SuppressKeyPress = true;
                StartCurrentInput();
            }
        };
        noteContentPanel.Controls.Add(noteContentLabel);
        noteContentPanel.Controls.Add(douyinNoteContent);

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

        var queuePage = new TabPage("任务队列")
        {
            AccessibleName = "任务队列页"
        };
        queuePage.Controls.Add(queueList);

        resultText.Dock = DockStyle.Fill;
        resultText.Multiline = true;
        resultText.ReadOnly = true;
        resultText.ScrollBars = ScrollBars.Vertical;
        resultText.AccessibleName = "状态与日志";
        resultText.AccessibleDescription = "显示本次运行的非敏感状态、错误和结果摘要。";
        resultText.Text = "尚未处理任何任务。";

        var statusPage = new TabPage("状态与日志")
        {
            AccessibleName = "状态与日志页"
        };
        statusPage.Controls.Add(resultText);

        taskTabs.Dock = DockStyle.Fill;
        taskTabs.AccessibleName = "任务与状态页签";
        taskTabs.TabIndex = 3;
        taskTabs.TabPages.Add(queuePage);
        taskTabs.TabPages.Add(statusPage);

        statusLabel.Text = "状态：空闲。请粘贴分享文本，或从资源管理器复制文件后按 Ctrl+V。";
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

        startButton.Text = "开始(&B)";
        startButton.AutoSize = true;
        startButton.Enabled = false;
        startButton.Click += (_, _) => StartCurrentInput();

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

        var privacyLabel = new Label
        {
            AutoSize = true,
            Text = "隐私提示：文件名可显示；读屏和日志不会自动朗读或记录完整路径。云端功能使用前会单独说明费用与隐私。",
            AccessibleName = "隐私提示"
        };

        layout.Controls.Add(linkLabel, 0, 0);
        layout.Controls.Add(linkInput, 0, 1);
        layout.Controls.Add(modePanel, 0, 2);
        layout.Controls.Add(noteContentPanel, 0, 3);
        layout.Controls.Add(taskTabs, 0, 4);
        layout.Controls.Add(statusLabel, 0, 5);
        layout.Controls.Add(progressBar, 0, 6);
        layout.Controls.Add(buttonPanel, 0, 7);
        layout.Controls.Add(privacyLabel, 0, 8);
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

    private void PasteClipboardContent()
    {
        if (IsProcessing)
        {
            ReportStatus("正在处理，已拒绝新粘贴。请等待任务结束或取消任务。", true);
            return;
        }

        try
        {
            var containsFileDrop = Clipboard.ContainsFileDropList();
            var containsUnicodeText = Clipboard.ContainsText(TextDataFormat.UnicodeText);
            var containsText = containsUnicodeText || Clipboard.ContainsText();
            switch (ClipboardFormatRouter.Choose(containsFileDrop, containsText))
            {
                case ClipboardPayloadKind.FileDrop:
                    linkInput.Clear();
                    PasteClipboardFiles();
                    return;
                case ClipboardPayloadKind.Text:
                    var text = containsUnicodeText
                        ? Clipboard.GetText(TextDataFormat.UnicodeText)
                        : Clipboard.GetText();
                    if (!string.IsNullOrWhiteSpace(text))
                    {
                        linkInput.Text = text.Trim();
                        linkInput.Focus();
                        linkInput.SelectionStart = linkInput.TextLength;
                        ReportStatus("已粘贴下载链接或平台分享文本。按 Enter 开始。", false);
                        UpdateButtons();
                        return;
                    }

                    break;
            }
        }
        catch (System.Runtime.InteropServices.ExternalException)
        {
            ReportStatus("暂时无法读取剪贴板，请稍后重试。", true);
            return;
        }

        ReportStatus("剪贴板中没有可用的文本或资源管理器文件。", true);
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

    private async void StartCurrentInput()
    {
        if (IsProcessing)
        {
            return;
        }

        if (!string.IsNullOrWhiteSpace(linkInput.Text))
        {
            await StartLinkTaskAsync();
            return;
        }

        if (queueList.Items.Count > 0)
        {
            ConfirmQueuedBatch();
            return;
        }

        ReportStatus("请先输入下载链接或粘贴资源管理器文件。", true);
        linkInput.Focus();
    }

    private async Task StartLinkTaskAsync()
    {
        var input = linkInput.Text.Trim();
        if (input.Length == 0)
        {
            return;
        }

        var selectedMode = processingMode.SelectedIndex;
        if (selectedMode == 3)
        {
            await CopyDirectLinkAsync(input);
            return;
        }

        using var completion = completionNotifier.Begin();
        DownloadProtocolOutcome? outcome = null;
        IsProcessing = true;
        previousBatchFinished = false;
        stopBatchRequested = false;
        batchCancellation = new CancellationTokenSource();
        batchCompletion = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        UpdateButtons();
        UpdateTrayStatus("正在下载");
        SetCurrentTaskProgress("正在启动下载工作进程并扫描链接", null);
        ReportStatus("正在启动下载工作进程并扫描链接。", false);
        try
        {
            var client = await EnsureWorkerClientAsync(batchCancellation.Token);
            var qualityPreference = "best";
            if (qualityAskItem.Checked)
            {
                ReportStatus("正在查询当前链接可用的品质和格式。", false);
                var qualityResponse = await client.SendAsync(
                    "quality.inspect",
                    new { text = input, playlist_mode = "single" },
                    batchCancellation.Token);
                var qualityOptions = qualityResponse.Payload.GetProperty("choices")
                    .EnumerateArray()
                    .Select(item => new DownloadChoiceOption(
                        item.GetProperty("value").GetString() ?? "best",
                        item.GetProperty("label").GetString() ?? "最高品质"))
                    .ToArray();
                using var qualityDialog = new QualitySelectionDialog(qualityOptions);
                if (qualityDialog.ShowDialog(this) != DialogResult.OK)
                {
                    ReportStatus("已取消本次下载；没有创建或修改媒体文件。", false);
                    return;
                }

                qualityPreference = qualityDialog.Preference;
            }

            var response = await client.SendAsync(
                "task.start",
                new
                {
                    text = input,
                    interactive_douyin_login = douyinLoginItem.Checked,
                    quality_preference = qualityPreference,
                    douyin_note_content = douyinNoteContent.SelectedIndex == 1
                        ? "images_and_audio"
                        : "audio_only"
                },
                batchCancellation.Token);
            outcome = await CompleteDownloadProtocolAsync(
                client,
                response,
                qualityPreference,
                batchCancellation.Token);
            if (outcome is not null)
            {
                SetCurrentResultDirectory(outcome.Paths.FirstOrDefault());
                var names = outcome.Paths.Select(Path.GetFileName).Where(name => !string.IsNullOrWhiteSpace(name));
                resultText.Text = outcome.Summary + Environment.NewLine +
                    string.Join(Environment.NewLine, names);
                ReportStatus(outcome.Summary, false);
            }
            else
            {
                ReportStatus("下载已由用户取消；没有开始后续转换。", false);
            }
        }
        catch (OperationCanceledException)
        {
            if (workerClient?.IsRunning == true)
            {
                await workerClient.CancelOrTerminateAsync(TimeSpan.FromSeconds(5));
            }
            ReportStatus("下载任务已取消；已完成的下载结果不会删除。", false);
        }
        catch (Exception exception)
        {
            ReportStatus($"下载工作进程错误：{ToActionableError(exception)}", true);
        }
        finally
        {
            IsProcessing = false;
            previousBatchFinished = true;
            batchCancellation.Dispose();
            batchCancellation = null;
            ResetProcessingMode();
            UpdateButtons();
            UpdateTrayStatus("空闲");
            SetCurrentTaskProgress("下载任务已经结束", null);
            batchCompletion?.TrySetResult();
        }

        if (outcome is not null && selectedMode > 0)
        {
            await BeginDownloadedPostProcessingAsync(outcome, selectedMode);
        }
    }

    private async Task StartCurrentWechatXiaoeCourseAsync()
    {
        if (IsProcessing)
        {
            ReportStatus("已有任务正在进行，请等待当前任务结束后再下载微信课程。", true);
            return;
        }

        using var completion = completionNotifier.Begin();
        ReportStatus("正在读取微信当前小鹅通已购课程目录；不会读取微信 Cookie。", false);
        WeChatXiaoeCourseSelection course;
        try
        {
            course = await xiaoeCourseAutomation.DiscoverCurrentCourseAsync(CancellationToken.None);
        }
        catch (Exception exception)
        {
            Activate();
            ReportStatus($"读取微信课程失败：{ToActionableError(exception)}", true);
            return;
        }

        Activate();
        var nonVideoCount = Math.Max(0, course.ReportedUpdateCount - course.Videos.Count);
        var updateDetail = course.ReportedUpdateCount > 0
            ? $"课程页显示已更新 {course.ReportedUpdateCount} 期，其中找到 {course.Videos.Count} 个直播视频"
            : $"找到 {course.Videos.Count} 个直播视频";
        var skippedDetail = nonVideoCount > 0
            ? $"；另外 {nonVideoCount} 个非视频项目不会下载"
            : "；图文和推荐内容不会下载";
        var confirmation = MessageBox.Show(
            this,
            $"课程：{course.Title}\r\n{updateDetail}{skippedDetail}。\r\n\r\n" +
            "软件将只操作这个课程目录中的直播回放。已有且通过校验的同名视频会跳过，以后再次运行可只补新发布课程。\r\n\r\n是否开始？",
            "确认下载当前已购课程",
            MessageBoxButtons.YesNo,
            MessageBoxIcon.Question,
            MessageBoxDefaultButton.Button2);
        if (confirmation != DialogResult.Yes)
        {
            ReportStatus("已取消微信课程下载；磁盘文件没有修改。", false);
            return;
        }

        IsProcessing = true;
        previousBatchFinished = false;
        stopBatchRequested = false;
        batchCancellation = new CancellationTokenSource();
        batchCompletion = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        UpdateButtons();
        UpdateTrayStatus("正在下载微信课程");
        SetCurrentTaskProgress("正在准备微信小鹅通课程下载", null);
        var paths = new List<string>();
        var skipped = 0;
        var failed = 0;
        string operationId = string.Empty;
        try
        {
            try
            {
                sleepInhibitor = SystemSleepInhibitor.Acquire();
            }
            catch (System.ComponentModel.Win32Exception)
            {
                sleepInhibitor = null;
                ReportStatus("无法临时阻止系统自动睡眠；请在课程下载期间保持电脑唤醒。", true);
            }

            var client = await EnsureWorkerClientAsync(batchCancellation.Token);
            var prepared = await client.SendAsync(
                "xiaoe.capture.prepare",
                new { },
                batchCancellation.Token);
            operationId = prepared.Payload.GetProperty("operation_id").GetString() ?? string.Empty;
            for (var index = 0; index < course.Videos.Count; index++)
            {
                batchCancellation.Token.ThrowIfCancellationRequested();
                var episode = course.Videos[index];
                var number = index + 1;
                SetCurrentTaskProgress(
                    $"正在处理第 {number}/{course.Videos.Count} 节：{episode.Title}",
                    (number - 1) * 100 / course.Videos.Count);
                ReportStatus(
                    $"正在处理第 {number}/{course.Videos.Count} 节：{episode.Title}",
                    false);
                try
                {
                    await client.SendAsync(
                        "xiaoe.capture.arm",
                        new { operation_id = operationId },
                        batchCancellation.Token);
                    await xiaoeCourseAutomation.OpenEpisodeAsync(
                        episode.Title,
                        batchCancellation.Token);
                    var response = await client.SendAsync(
                        "xiaoe.capture.download",
                        new
                        {
                            operation_id = operationId,
                            course_title = course.Title,
                            episode_title = episode.Title,
                            episode_index = number,
                            episode_total = course.Videos.Count,
                            page_url = course.PageUrl
                        },
                        batchCancellation.Token);
                    var path = response.Payload.GetProperty("path").GetString();
                    if (!string.IsNullOrWhiteSpace(path))
                    {
                        paths.Add(path);
                    }
                    if (response.Payload.TryGetProperty("skipped", out var skippedElement) &&
                        skippedElement.GetBoolean())
                    {
                        skipped++;
                    }
                }
                catch (OperationCanceledException)
                {
                    throw;
                }
                catch (Exception exception)
                {
                    failed++;
                    ReportStatus(
                        $"第 {number} 节未完成：{ToActionableError(exception)}；将继续下一节。",
                        true);
                }
                finally
                {
                    try
                    {
                        await xiaoeCourseAutomation.ReturnToCatalogAsync(CancellationToken.None);
                    }
                    catch (Exception exception)
                    {
                        ReportStatus($"返回微信课程目录失败：{ToActionableError(exception)}", true);
                    }
                }
            }

            if (paths.Count > 0)
            {
                currentResultDirectory = Path.GetDirectoryName(paths[0]);
            }
            var downloaded = paths.Count - skipped;
            var summary = $"微信课程处理结束：新下载 {downloaded} 节，已有有效文件跳过 {skipped} 节，失败 {failed} 节。";
            resultText.Text = summary + Environment.NewLine +
                string.Join(Environment.NewLine, paths.Select(Path.GetFileName));
            ReportStatus(summary, failed > 0);
        }
        catch (OperationCanceledException)
        {
            if (workerClient?.IsRunning == true)
            {
                await workerClient.CancelOrTerminateAsync(TimeSpan.FromSeconds(5));
            }
            ReportStatus("微信课程下载已取消；已有成功文件保持不变。", false);
        }
        catch (Exception exception)
        {
            ReportStatus($"微信课程下载失败：{ToActionableError(exception)}", true);
        }
        finally
        {
            if (!string.IsNullOrWhiteSpace(operationId) && workerClient?.IsRunning == true)
            {
                try
                {
                    await ReleaseWorkerOperationAsync(workerClient, operationId, CancellationToken.None);
                }
                catch (Exception)
                {
                    // Worker disposal below also clears all in-memory signed media state.
                }
            }
            sleepInhibitor?.Dispose();
            sleepInhibitor = null;
            IsProcessing = false;
            previousBatchFinished = true;
            batchCancellation?.Dispose();
            batchCancellation = null;
            UpdateButtons();
            UpdateTrayStatus("空闲");
            SetCurrentTaskProgress("微信课程下载任务已经结束", null);
            batchCompletion?.TrySetResult();
            Show();
            WindowState = FormWindowState.Normal;
            Activate();
        }
    }

    private async Task CopyDirectLinkAsync(string input)
    {
        using var completion = completionNotifier.Begin();
        IsProcessing = true;
        previousBatchFinished = false;
        stopBatchRequested = false;
        currentResultDirectory = null;
        batchCancellation = new CancellationTokenSource();
        batchCompletion = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        UpdateButtons();
        UpdateTrayStatus("正在解析直连");
        SetCurrentTaskProgress("正在解析作品直连", null);
        ReportStatus("正在解析作品直连；不会下载媒体文件。", false);
        try
        {
            var client = await EnsureWorkerClientAsync(batchCancellation.Token);
            var response = await client.SendAsync(
                "direct_link.copy",
                new
                {
                    text = input,
                    interactive_douyin_login = douyinLoginItem.Checked
                },
                batchCancellation.Token);
            var copied = response.Payload.TryGetProperty("copied", out var copiedElement) &&
                copiedElement.GetBoolean();
            var mediaKind = response.Payload.TryGetProperty("media_kind", out var kindElement)
                ? kindElement.GetString() ?? string.Empty
                : string.Empty;
            if (!copied)
            {
                throw new WorkerProtocolException(
                    "direct_link_not_copied",
                    "下载工作进程没有确认剪贴板写入成功。");
            }

            var detail = mediaKind == "audio"
                ? "已复制最佳音频直连。"
                : "已复制最佳音画合一直连。";
            var summary = $"解析成功，直连已复制到系统剪贴板。{detail}直连可能短期失效，请及时使用。";
            resultText.Text = summary;
            ReportStatus(summary, false);
        }
        catch (OperationCanceledException)
        {
            if (workerClient?.IsRunning == true)
            {
                await workerClient.CancelOrTerminateAsync(TimeSpan.FromSeconds(5));
            }
            ReportStatus("直连解析已取消；没有下载或创建媒体文件。", false);
        }
        catch (Exception exception)
        {
            ReportStatus($"获取解析直连失败：{ToActionableError(exception)}", true);
        }
        finally
        {
            IsProcessing = false;
            previousBatchFinished = true;
            batchCancellation.Dispose();
            batchCancellation = null;
            ResetProcessingMode();
            UpdateButtons();
            UpdateTrayStatus("空闲");
            SetCurrentTaskProgress("直连解析任务已经结束", null);
            batchCompletion?.TrySetResult();
        }
    }

    private async Task<DownloadProtocolOutcome?> CompleteDownloadProtocolAsync(
        FeichuanWorkerClient client,
        WorkerMessage response,
        string qualityPreference,
        CancellationToken cancellationToken)
    {
        var wasBatch = false;
        while (true)
        {
            var kind = response.Payload.GetProperty("kind").GetString() ?? string.Empty;
            if (kind == "download")
            {
                var paths = response.Payload.GetProperty("paths")
                    .EnumerateArray()
                    .Select(item => item.GetString())
                    .Where(path => !string.IsNullOrWhiteSpace(path))
                    .Cast<string>()
                    .ToArray();
                var succeeded = response.Payload.TryGetProperty("succeeded", out var succeededElement)
                    ? succeededElement.GetInt32()
                    : paths.Length;
                var skipped = response.Payload.TryGetProperty("skipped", out var skippedElement)
                    ? skippedElement.GetInt32()
                    : 0;
                var failed = response.Payload.TryGetProperty("failed", out var failedElement)
                    ? failedElement.GetInt32()
                    : 0;
                var partialSuccess = response.Payload.TryGetProperty("partial_success", out var partialElement) &&
                    partialElement.ValueKind == System.Text.Json.JsonValueKind.True;
                var warnings = response.Payload.TryGetProperty("warnings", out var warningsElement)
                    ? warningsElement.EnumerateArray()
                        .Select(item => item.GetString())
                        .Where(value => !string.IsNullOrWhiteSpace(value))
                        .Cast<string>()
                        .ToArray()
                    : [];
                var resultSummary = partialSuccess
                    ? $"下载部分成功：已保留 {paths.Length} 个成功文件。{string.Join("；", warnings)}"
                    : $"下载结束：成功 {succeeded} 个，跳过 {skipped} 个，失败 {failed} 个。源下载文件保持不变。";
                return new DownloadProtocolOutcome(
                    paths,
                    wasBatch,
                    resultSummary,
                    partialSuccess);
            }

            var operationId = response.Payload.GetProperty("operation_id").GetString() ?? string.Empty;
            if (kind == "batch_confirmation")
            {
                wasBatch = true;
                var scan = response.Payload.GetProperty("scan");
                var count = scan.GetProperty("unique_count").GetInt32();
                var complete = scan.GetProperty("enumeration_complete").GetBoolean();
                var title = scan.GetProperty("source_title").GetString();
                if (string.IsNullOrWhiteSpace(title))
                {
                    title = scan.GetProperty("author").GetString();
                }

                var options = response.Payload.GetProperty("choices")
                    .EnumerateArray()
                    .Select(item => item.GetString() ?? string.Empty)
                    .Where(value => value.Length > 0)
                    .Select(value => new DownloadChoiceOption(value, DownloadChoiceLabel(value)))
                    .ToArray();
                var summary = $"扫描完成：{(string.IsNullOrWhiteSpace(title) ? "批量来源" : title)}，" +
                    $"发现 {count} 个，枚举{(complete ? "完整" : "不完整")}。";
                using var dialog = new BatchDownloadConfirmationDialog(summary, options);
                if (dialog.ShowDialog(this) != DialogResult.OK)
                {
                    await ReleaseWorkerOperationAsync(client, operationId, cancellationToken);
                    return null;
                }

                response = await client.SendAsync(
                    "prepared.download",
                    new
                    {
                        operation_id = operationId,
                        choice = dialog.Choice,
                        quality_preference = qualityPreference
                    },
                    cancellationToken);
                continue;
            }

            if (kind == "generic_confirmation")
            {
                wasBatch = true;
                var inspectionError = response.Payload.GetProperty("inspection_error").GetString() ?? string.Empty;
                var title = string.Empty;
                var count = 0;
                var preview = Array.Empty<string>();
                var inspection = response.Payload.GetProperty("inspection");
                if (inspection.ValueKind == System.Text.Json.JsonValueKind.Object)
                {
                    title = inspection.GetProperty("title").GetString() ?? string.Empty;
                    count = inspection.GetProperty("count").GetInt32();
                    preview = inspection.GetProperty("entries_preview")
                        .EnumerateArray()
                        .Select(item => item.GetString() ?? string.Empty)
                        .Where(item => item.Length > 0)
                        .ToArray();
                }

                using var dialog = new GenericDownloadConfirmationDialog(
                    title,
                    count,
                    preview,
                    inspectionError);
                if (dialog.ShowDialog(this) != DialogResult.OK)
                {
                    await ReleaseWorkerOperationAsync(client, operationId, cancellationToken);
                    return null;
                }

                response = await client.SendAsync(
                    "generic.download",
                    new
                    {
                        operation_id = operationId,
                        playlist_mode = dialog.PlaylistMode,
                        quality_preference = qualityPreference
                    },
                    cancellationToken);
                continue;
            }

            throw new WorkerProtocolException("unexpected_result", "下载工作进程返回了未知结果。");
        }
    }

    private static string DownloadChoiceLabel(string value) => value switch
    {
        "incremental" => "增量下载：跳过已有有效结果",
        "redownload_all" => "重新下载全部：仍不覆盖已有文件",
        "retry_failed" => "只重试失败项",
        "discovered_only" => "枚举不完整：只下载已发现项目",
        _ => value
    };

    private static async Task ReleaseWorkerOperationAsync(
        FeichuanWorkerClient client,
        string operationId,
        CancellationToken cancellationToken)
    {
        if (!string.IsNullOrWhiteSpace(operationId))
        {
            await client.SendAsync(
                "operation.release",
                new { operation_id = operationId },
                cancellationToken);
        }
    }

    private async Task BeginDownloadedPostProcessingAsync(
        DownloadProtocolOutcome outcome,
        int selectedMode)
    {
        var plans = DownloadPipelinePlanner.Create(outcome.Paths);
        var processable = plans.Where(plan => plan.Action != DownloadedMediaAction.Skip).ToArray();
        var skipped = plans.Where(plan => plan.Action == DownloadedMediaAction.Skip).ToArray();
        if (skipped.Length > 0)
        {
            resultText.AppendText(Environment.NewLine + Environment.NewLine +
                string.Join(Environment.NewLine, skipped.Select(plan =>
                    $"{Path.GetFileName(plan.Path)}：已跳过后续处理；{plan.Reason}。")));
        }

        if (processable.Length == 0)
        {
            ReportStatus(
                outcome.IsPartialSuccess
                    ? outcome.Summary
                    : "下载成功，但结果中没有可转换的视频或可识别的 MP3；文件已保留。",
                true);
            return;
        }

        if (outcome.WasBatch && selectedMode != 2 && MessageBox.Show(
                this,
                $"批量下载已经结束，共有 {processable.Length} 个结果可继续处理。是否现在开始“{ProcessingModeName(selectedMode)}”？",
                "确认后续处理",
                MessageBoxButtons.YesNo,
                MessageBoxIcon.Question,
                MessageBoxDefaultButton.Button2) != DialogResult.Yes)
        {
            ReportStatus("下载成功；已取消后续转换，下载结果全部保留。", false);
            return;
        }

        queueList.Items.Clear();
        foreach (var plan in processable)
        {
            var kind = plan.Action switch
            {
                DownloadedMediaAction.UseExistingMp3 => MediaKind.Mp3,
                DownloadedMediaAction.ConvertAudio => MediaKind.Audio,
                _ => MediaKind.Video
            };
            queueList.Items.Add(CreateQueueRow(new QueueItem(plan.Path, kind)));
        }

        if (queueList.Items.Count > 0)
        {
            queueList.Items[0].Selected = true;
            queueList.Items[0].Focused = true;
        }

        var decision = selectedMode == 2
            ? new PasteDecision(true, true, true, false)
            : new PasteDecision(true, false, false, false);
        if (selectedMode == 2)
        {
            await StartCloudBatchAsync(decision);
        }
        else
        {
            await StartLocalBatchAsync(decision);
        }
        if (outcome.IsPartialSuccess)
        {
            resultText.AppendText(Environment.NewLine + Environment.NewLine +
                "下载阶段：" + outcome.Summary);
            ReportStatus(outcome.Summary, true);
        }
    }

    private static string ProcessingModeName(int index) => index switch
    {
        1 => "下载后转换为 MP3",
        2 => "下载、转换 MP3 并生成 TXT",
        3 => "获取解析直连",
        _ => "只下载"
    };

    private sealed record DownloadProtocolOutcome(
        IReadOnlyList<string> Paths,
        bool WasBatch,
        string Summary,
        bool IsPartialSuccess = false);

    private async Task<FeichuanWorkerClient> EnsureWorkerClientAsync(CancellationToken cancellationToken)
    {
        if (workerClient?.IsRunning == true)
        {
            return workerClient;
        }

        if (workerClient is not null)
        {
            await workerClient.DisposeAsync();
            workerClient = null;
        }

        var installedWorker = Path.Combine(AppContext.BaseDirectory, "feichuan-worker.exe");
        WorkerLaunchOptions options;
        if (File.Exists(installedWorker))
        {
            options = new WorkerLaunchOptions(installedWorker, WorkingDirectory: AppContext.BaseDirectory);
        }
        else
        {
            var repositoryRoot = FindDevelopmentRepositoryRoot();
            if (repositoryRoot is null)
            {
                throw new WorkerProcessException("找不到 feichuan-worker.exe，请重新安装或重新构建工作进程。");
            }

            var workerRoot = Path.Combine(repositoryRoot, "worker");
            var workerMain = Path.Combine(workerRoot, "src", "worker_main.py");
            options = new WorkerLaunchOptions(
                "python",
                [workerMain],
                workerRoot,
                new Dictionary<string, string>
                {
                    ["PYTHONPATH"] = Path.Combine(workerRoot, "src"),
                    ["PYTHONDONTWRITEBYTECODE"] = "1",
                    ["FEICHUAN_AUTO_UPDATE_CORE"] = "0",
                    ["FEICHUAN_SOFTWARE_UPDATE_ENDPOINT"] = string.Empty
                });
        }

        var client = new FeichuanWorkerClient(options);
        client.EventReceived += (_, message) =>
        {
            if (!IsDisposed && IsHandleCreated)
            {
                BeginInvoke(() => ApplyWorkerEvent(message));
            }
        };
        client.DiagnosticReceived += (_, line) =>
        {
            if (!IsDisposed && IsHandleCreated)
            {
                BeginInvoke(() => ReportStatus($"下载内核：{line}", true));
            }
        };
        try
        {
            await client.StartAsync(cancellationToken);
        }
        catch
        {
            await client.DisposeAsync();
            throw;
        }

        workerClient = client;
        return client;
    }

    private async Task LoadWorkerPreferencesAsync()
    {
        try
        {
            var client = await EnsureWorkerClientAsync(CancellationToken.None);
            var quality = await client.SendAsync("quality_mode.get", new { }, CancellationToken.None);
            var askEachTime = string.Equals(
                quality.Payload.GetProperty("mode").GetString(),
                "ask_each_time",
                StringComparison.Ordinal);
            qualityBestItem.Checked = !askEachTime;
            qualityAskItem.Checked = askEachTime;

            var login = await client.SendAsync("douyin_login.status", new { }, CancellationToken.None);
            douyinLoginItem.Checked = login.Payload.TryGetProperty("saved", out var saved) && saved.GetBoolean();
        }
        catch (Exception exception) when (
            exception is WorkerProcessException or WorkerProtocolException or IOException)
        {
            qualityBestItem.Checked = true;
            qualityAskItem.Checked = false;
            ReportStatus($"下载工作进程暂时不可用：{ToActionableError(exception)}", true);
        }
    }

    private async Task ChooseDownloadFolderAsync()
    {
        if (IsProcessing)
        {
            ReportStatus("任务进行中不能更改下载文件夹。", true);
            return;
        }

        try
        {
            var client = await EnsureWorkerClientAsync(CancellationToken.None);
            var current = await client.SendAsync("download_directory.get", new { }, CancellationToken.None);
            using var dialog = new FolderBrowserDialog
            {
                Description = "请选择飞船下载文件夹。已有媒体不会被删除或覆盖。",
                UseDescriptionForTitle = true,
                ShowNewFolderButton = true,
                SelectedPath = current.Payload.GetProperty("path").GetString() ?? string.Empty
            };
            if (dialog.ShowDialog(this) != DialogResult.OK)
            {
                return;
            }

            var changed = await client.SendAsync(
                "download_directory.set",
                new { path = dialog.SelectedPath },
                CancellationToken.None);
            currentResultDirectory = changed.Payload.GetProperty("path").GetString();
            ReportStatus("下载文件夹已保存。", false);
        }
        catch (Exception exception)
        {
            ReportStatus($"无法设置下载文件夹：{ToActionableError(exception)}", true);
        }
    }

    private async Task SetQualityModeAsync(bool askEachTime)
    {
        if (IsProcessing)
        {
            ReportStatus("任务进行中不能更改品质询问方式。", true);
            return;
        }

        try
        {
            var client = await EnsureWorkerClientAsync(CancellationToken.None);
            var response = await client.SendAsync(
                "quality_mode.set",
                new { mode = askEachTime ? "ask_each_time" : "best" },
                CancellationToken.None);
            askEachTime = string.Equals(
                response.Payload.GetProperty("mode").GetString(),
                "ask_each_time",
                StringComparison.Ordinal);
            qualityBestItem.Checked = !askEachTime;
            qualityAskItem.Checked = askEachTime;
            ReportStatus(askEachTime ? "每次下载前会询问品质。" : "下载品质已设为默认最高品质。", false);
        }
        catch (Exception exception)
        {
            ReportStatus($"无法保存品质设置：{ToActionableError(exception)}", true);
        }
    }

    private async Task ClearDouyinLoginAsync()
    {
        if (IsProcessing)
        {
            ReportStatus("任务进行中不能清除抖音专用登录资料。", true);
            return;
        }

        if (MessageBox.Show(
                this,
                "这会清除软件专用的抖音浏览器资料，不影响日常浏览器。是否继续？",
                "清除抖音专用登录",
                MessageBoxButtons.YesNo,
                MessageBoxIcon.Warning,
                MessageBoxDefaultButton.Button2) != DialogResult.Yes)
        {
            return;
        }

        try
        {
            var client = await EnsureWorkerClientAsync(CancellationToken.None);
            var response = await client.SendAsync("douyin_login.clear", new { }, CancellationToken.None);
            douyinLoginItem.Checked = false;
            var removed = response.Payload.TryGetProperty("removed", out var value) && value.GetBoolean();
            ReportStatus(removed ? "抖音专用登录资料已清除。" : "没有找到已保存的抖音专用登录资料。", false);
        }
        catch (Exception exception)
        {
            ReportStatus($"无法清除抖音专用登录资料：{ToActionableError(exception)}", true);
        }
    }

    private async Task CheckCoreUpdateAsync()
    {
        if (IsProcessing)
        {
            ReportStatus("任务进行中不能检查下载核心更新。", true);
            return;
        }

        try
        {
            var client = await EnsureWorkerClientAsync(CancellationToken.None);
            ReportStatus("正在手动检查下载核心 stable 更新。", false);
            var response = await client.SendAsync(
                "core_update.check",
                new { install = false },
                CancellationToken.None);
            var message = response.Payload.GetProperty("message").GetString() ?? "检查结束。";
            var available = response.Payload.TryGetProperty("available", out var value) && value.GetBoolean();
            if (!available)
            {
                MessageBox.Show(this, message, "下载核心更新", MessageBoxButtons.OK, MessageBoxIcon.Information);
                ReportStatus(message, false);
                return;
            }

            if (MessageBox.Show(
                    this,
                    message + "\r\n\r\n是否下载、校验并安装这个 stable 核心？",
                    "确认更新下载核心",
                    MessageBoxButtons.YesNo,
                    MessageBoxIcon.Question,
                    MessageBoxDefaultButton.Button2) != DialogResult.Yes)
            {
                ReportStatus("已取消下载核心更新，当前核心保持不变。", false);
                return;
            }

            response = await client.SendAsync(
                "core_update.check",
                new { install = true },
                CancellationToken.None);
            message = response.Payload.GetProperty("message").GetString() ?? "更新结束。";
            var updated = response.Payload.TryGetProperty("updated", out value) && value.GetBoolean();
            MessageBox.Show(
                this,
                message,
                "下载核心更新",
                MessageBoxButtons.OK,
                updated ? MessageBoxIcon.Information : MessageBoxIcon.Warning);
            ReportStatus(message, !updated);
        }
        catch (Exception exception)
        {
            ReportStatus($"检查下载核心失败：{ToActionableError(exception)}", true);
        }
    }

    private async Task CheckSoftwareUpdateAsync()
    {
        if (IsProcessing)
        {
            ReportStatus("任务进行中不能检查软件更新。", true);
            return;
        }

        try
        {
            var client = await EnsureWorkerClientAsync(CancellationToken.None);
            ReportStatus("正在手动检查软件更新。", false);
            var response = await client.SendAsync("software_update.check", new { }, CancellationToken.None);
            var message = response.Payload.GetProperty("message").GetString() ?? "检查结束。";
            var notes = response.Payload.TryGetProperty("release_notes", out var notesValue)
                ? notesValue.GetString()
                : null;
            var detail = string.IsNullOrWhiteSpace(notes)
                ? message
                : message + "\r\n\r\n" + notes;
            MessageBox.Show(
                this,
                detail,
                "软件更新",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
            ReportStatus(message, false);
        }
        catch (Exception exception)
        {
            ReportStatus($"检查软件更新失败：{ToActionableError(exception)}", true);
        }
    }

    private void ApplyWorkerEvent(WorkerMessage message)
    {
        if (message.Type == "task.log")
        {
            var line = message.Payload.TryGetProperty("message", out var lineElement)
                ? lineElement.GetString()
                : null;
            if (!string.IsNullOrWhiteSpace(line))
            {
                resultText.AppendText(Environment.NewLine + line);
                if (line.StartsWith("正在下载抖音媒体", StringComparison.Ordinal))
                {
                    SetCurrentTaskProgress(line, 0);
                }
                else if (
                    line.Contains("合并音视频", StringComparison.Ordinal) ||
                    line.StartsWith("首次音视频合并未成功", StringComparison.Ordinal))
                {
                    SetCurrentTaskProgress(line, null);
                }
            }

            return;
        }

        if (message.Type != "task.progress")
        {
            return;
        }

        var stage = message.Payload.TryGetProperty("stage", out var stageElement)
            ? stageElement.GetString() ?? "processing"
            : "processing";
        var status = message.Payload.TryGetProperty("message", out var messageElement)
            ? messageElement.GetString() ?? string.Empty
            : string.Empty;
        int? currentPercentage = null;
        if (message.Payload.TryGetProperty("overall_percent", out var percentElement) &&
            percentElement.ValueKind == System.Text.Json.JsonValueKind.Number &&
            percentElement.TryGetDouble(out var percent))
        {
            progressBar.Value = Math.Clamp((int)Math.Round(percent), 0, 100);
            progressBar.AccessibleDescription = $"下载任务进度 {progressBar.Value}%。";
            currentPercentage = progressBar.Value;
        }

        var current = message.Payload.TryGetProperty("current", out var currentElement)
            ? currentElement.GetInt32()
            : 0;
        var total = message.Payload.TryGetProperty("total", out var totalElement)
            ? totalElement.GetInt32()
            : 0;
        var summary = string.IsNullOrWhiteSpace(status)
            ? total > 0 ? $"下载阶段 {stage}，当前 {current}/{total}。" : $"下载阶段 {stage}。"
            : status;
        SetCurrentTaskProgress(summary, currentPercentage);
        statusLabel.Text = $"状态：{summary}";
        statusLabel.AccessibleDescription = statusLabel.Text;
    }

    private static string? FindDevelopmentRepositoryRoot()
    {
        foreach (var start in new[] { Environment.CurrentDirectory, AppContext.BaseDirectory })
        {
            var current = new DirectoryInfo(start);
            while (current is not null)
            {
                if (File.Exists(Path.Combine(current.FullName, "worker", "src", "worker_main.py")))
                {
                    return current.FullName;
                }

                current = current.Parent;
            }
        }

        return null;
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
        startButton.Enabled = !IsProcessing &&
            (!string.IsNullOrWhiteSpace(linkInput.Text) || queueList.Items.Count > 0);
        removeButton.Enabled = !IsProcessing && queueList.SelectedItems.Count > 0;
        cancelButton.Enabled = IsProcessing;
    }

    private void ResetProcessingMode()
    {
        if (processingMode.Items.Count > 0)
        {
            processingMode.SelectedIndex = 0;
        }
        if (douyinNoteContent.Items.Count > 0)
        {
            douyinNoteContent.SelectedIndex = 0;
        }
    }

    private async Task LoadSettingsAsync()
    {
        try
        {
            settings = await settingsStore.LoadAsync(CancellationToken.None);
            if (settings.Mp3BitrateKbps is not (128 or 192 or 256 or 320))
            {
                settings = settings with { Mp3BitrateKbps = 192 };
            }
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or System.Text.Json.JsonException)
        {
            settings = new AppSettings();
            ReportStatus("设置文件无法读取，已在本次运行使用默认 192 kbps；没有覆盖原设置。", true);
        }
    }

    private async Task ShowFirstRunHelpIfNeededAsync()
    {
        if (settings.HasShownVersion1Help)
        {
            return;
        }

        using var dialog = new HelpDialog(HelpDialog.LoadContent());
        dialog.ShowDialog(this);
        settings = settings with { HasShownVersion1Help = true };
        try
        {
            await settingsStore.SaveAsync(settings, CancellationToken.None);
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            ReportStatus("无法保存首次说明已读状态；下次启动可能再次显示使用说明。", true);
        }
    }

    private async Task StartLocalBatchAsync(PasteDecision decision)
    {
        using var completion = completionNotifier.Begin();
        var allItems = queueList.Items.Cast<ListViewItem>()
            .Select(row => row.Tag)
            .OfType<QueueItem>()
            .ToArray();
        var scopedItems = decision.FirstItemOnly ? allItems.Take(1).ToArray() : allItems;
        if (scopedItems.Length == 0)
        {
            return;
        }

        var outputSettingsSnapshot = settings;
        Dictionary<Guid, string> outputDirectories;
        try
        {
            outputDirectories = scopedItems
                .Where(item => item.Kind != MediaKind.Mp3)
                .ToDictionary(
                    item => item.Id,
                    item => OutputDirectoryPolicy.ResolveForSource(outputSettingsSnapshot, item.SourcePath));
        }
        catch (Exception exception) when (
            exception is InvalidDataException or IOException or UnauthorizedAccessException or ArgumentException)
        {
            ReportStatus(ToActionableError(exception), true);
            return;
        }

        currentResultDirectory = outputDirectories.Count > 0
            ? outputDirectories.Values.First()
            : Path.GetDirectoryName(scopedItems[0].SourcePath);

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
                SetCurrentTaskProgress("正在检查媒体格式和音轨", null);
                ReportStatus($"第 {processedCount + 1} 项，共 {scopedItems.Length} 项：{item.FileName}，开始探测。", false);

                var progress = new Progress<int>(percentage =>
                {
                    item.Stage = JobStage.ConvertingMp3;
                    item.StepDetail = $"正在转换第一条音轨，{percentage}%";
                    progressBar.Value = Math.Clamp(percentage, 0, 100);
                    progressBar.AccessibleDescription = $"{item.FileName}，转换 MP3，{percentage}%。";
                    SetCurrentTaskProgress("正在转换 MP3", progressBar.Value);
                    statusLabel.Text = $"状态：第 {processedCount + 1} 项，共 {scopedItems.Length} 项，正在转换 {item.FileName}，{percentage}%。";
                    statusLabel.AccessibleDescription = statusLabel.Text;
                    UpdateQueueRow(item, notifyAccessibility: false);
                });

                try
                {
                    var processingResult = await localVideoProcessor.ProcessAsync(
                        item,
                        settings.Mp3BitrateKbps,
                        outputDirectories[item.Id],
                        progress,
                        currentItemCancellation.Token);
                    SetCurrentResultDirectory(processingResult.Mp3Path);
                    succeeded++;
                    progressBar.Value = 100;
                    progressBar.AccessibleDescription = $"{item.FileName}，转换完成，100%。";
                    UpdateQueueRow(item);
                    ReportStatus(
                        $"{item.FileName} 转换成功，MP3 已保存在{DescribeOutputLocation(outputSettingsSnapshot)}。",
                        false);
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
            SetCurrentTaskProgress("当前任务已经结束", null);
            ResetProcessingMode();
            UpdateButtons();

            var summary = $"本批结束：成功 {succeeded} 个，失败 {failed} 个，跳过 {skipped} 个，已取消 {cancelled} 个，未开始 {notStarted} 个。成功的 MP3 保存在{DescribeOutputLocation(outputSettingsSnapshot)}。";
            resultText.Text = summary + Environment.NewLine + Environment.NewLine +
                string.Join(Environment.NewLine, scopedItems.Select(item =>
                    $"{item.FileName}：{(string.IsNullOrWhiteSpace(item.ResultMessage) ? item.StepDetail : item.ResultMessage)}"));
            ReportStatus(summary, failed > 0);
            UpdateTrayStatus(failed > 0 ? "批次结束，有失败" : "批次完成");
            batchCompletion?.TrySetResult();
        }
    }

    private async Task StartCloudBatchAsync(PasteDecision decision)
    {
        using var completion = completionNotifier.Begin();
        var allItems = queueList.Items.Cast<ListViewItem>()
            .Select(row => row.Tag)
            .OfType<QueueItem>()
            .ToArray();
        var scopedItems = decision.FirstItemOnly ? allItems.Take(1).ToArray() : allItems;
        var outputSettingsSnapshot = settings;
        string? customOutputDirectory = null;
        if (scopedItems.Length > 0 && OutputDirectoryPolicy.UsesCustomDirectory(outputSettingsSnapshot))
        {
            try
            {
                customOutputDirectory = OutputDirectoryPolicy.ResolveForSource(
                    outputSettingsSnapshot,
                    scopedItems[0].SourcePath);
            }
            catch (Exception exception) when (
                exception is InvalidDataException or IOException or UnauthorizedAccessException or ArgumentException)
            {
                ReportStatus(ToActionableError(exception), true);
                return;
            }
        }

        var credentials = await EnsureCloudCredentialsAsync();
        if (credentials is null)
        {
            ReportStatus("已取消腾讯云配置；没有上传文件或提交识别。", false);
            return;
        }

        if (scopedItems.Length > 0)
        {
            SetCurrentResultDirectory(customOutputDirectory ?? scopedItems[0].SourcePath);
        }
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
        ReportStatus(
            $"已按所选转文字任务自动继续腾讯云上传和识别。本批估算时长：{FormatDuration(estimatedDuration)}；本机本月已成功识别估算：{FormatDuration(currentUsage)}。费用和额度以腾讯云控制台为准。",
            false);
        if (currentUsage + estimatedDuration >= CloudLimits.MonthlyFreeAllowance)
        {
            ReportStatus("本机估算已达到每月 10 小时参考线，将按所选任务继续识别；实际费用和额度以腾讯云控制台为准。", false);
        }

        var fallbackDirectory = customOutputDirectory is null
            ? ChooseMp3FallbackDirectoryIfNeeded(scopedItems, decision)
            : (Cancelled: false, Path: (string?)null);
        if (fallbackDirectory.Cancelled)
        {
            ReportStatus("现有 MP3 的原目录不可写，且未选择备用目录；没有开始云端批次。", true);
            return;
        }
        if (!string.IsNullOrWhiteSpace(fallbackDirectory.Path))
        {
            currentResultDirectory = fallbackDirectory.Path;
        }

        Dictionary<Guid, string> outputDirectories;
        try
        {
            outputDirectories = scopedItems
                .Where(item => item.Kind != MediaKind.Mp3 || decision.TranscribeMp3)
                .ToDictionary(
                    item => item.Id,
                    item => customOutputDirectory ?? ResolveDefaultCloudOutputDirectory(
                        item,
                        fallbackDirectory.Path));
        }
        catch (Exception exception) when (
            exception is InvalidDataException or IOException or UnauthorizedAccessException or ArgumentException)
        {
            ReportStatus(ToActionableError(exception), true);
            return;
        }
        if (outputDirectories.Count > 0)
        {
            currentResultDirectory = outputDirectories.Values.First();
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
                        SetCurrentTaskProgress(update.Message, progressBar.Value);
                    }
                    else
                    {
                        progressBar.Value = 0;
                        progressBar.AccessibleDescription = $"{item.FileName}，{update.Message}；该阶段没有真实百分比。";
                        SetCurrentTaskProgress(update.Message, null);
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
                        processingResult = await cloudProcessor.ProcessMp3Async(
                            item,
                            outputDirectories[item.Id],
                            progress,
                            currentItemCancellation.Token);
                    }
                    else if (shouldTranscribe)
                    {
                        processingResult = await cloudProcessor.ProcessVideoAsync(
                            item,
                            settings.Mp3BitrateKbps,
                            outputDirectories[item.Id],
                            progress,
                            currentItemCancellation.Token);
                    }
                    else
                    {
                        await localVideoProcessor.ProcessAsync(
                            item,
                            settings.Mp3BitrateKbps,
                            outputDirectories[item.Id],
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
            SetCurrentTaskProgress("当前任务已经结束", null);
            ResetProcessingMode();
            UpdateButtons();

            var summary = $"本批结束：成功 {succeeded} 个，部分成功 {partiallySucceeded} 个，失败 {failed} 个，跳过 {skipped} 个，已取消 {cancelled} 个，待恢复 {recoverable} 个，未开始 {notStarted} 个。";
            resultText.Text = summary + Environment.NewLine + Environment.NewLine +
                string.Join(Environment.NewLine, scopedItems.Select(item =>
                    $"{item.FileName}：{(string.IsNullOrWhiteSpace(item.ResultMessage) ? item.StepDetail : item.ResultMessage)}"));
            ReportStatus(summary, failed > 0 || recoverable > 0);
            UpdateTrayStatus(failed > 0 || recoverable > 0 ? "批次结束，需要检查" : "批次完成");
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

    private static bool IsDirectoryWritable(string directory) => OutputDirectoryPolicy.IsWritable(directory);

    private static string ResolveDefaultCloudOutputDirectory(QueueItem item, string? fallbackDirectory)
    {
        var sourceDirectory = Path.GetDirectoryName(item.SourcePath)
            ?? throw new IOException("无法确定源文件所在目录。 ");
        if (item.Kind == MediaKind.Mp3 && !IsDirectoryWritable(sourceDirectory))
        {
            if (string.IsNullOrWhiteSpace(fallbackDirectory))
            {
                throw new IOException("MP3 所在目录不可写，且没有选择备用结果目录。 ");
            }

            return OutputDirectoryPolicy.ValidateCustomDirectory(fallbackDirectory);
        }

        return OutputDirectoryPolicy.ResolveForSource(new AppSettings(), item.SourcePath);
    }

    private static string DescribeOutputLocation(AppSettings settings) =>
        OutputDirectoryPolicy.UsesCustomDirectory(settings)
            ? "设置的统一结果目录"
            : "各自源文件旁边";

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

        using var completion = completionNotifier.Begin();
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
                        SetCurrentTaskProgress(update.Message, update.Percentage);
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
            SetCurrentTaskProgress("当前任务已经结束", null);
            ResetProcessingMode();
            UpdateButtons();
            UpdateTrayStatus("空闲");
            batchCompletion?.TrySetResult();
        }
    }

    private void ConfigureTrayIcon()
    {
        var trayMenu = new ContextMenuStrip();
        var showItem = new ToolStripMenuItem("显示主窗口", null, (_, _) => ShowMainWindow());
        var exitItem = new ToolStripMenuItem("退出软件", null, async (_, _) => await ExitFromTrayAsync());
        trayMenu.Items.AddRange([showItem, exitItem]);

        trayIcon.Icon = SystemIcons.Application;
        trayIcon.Text = "飞船下载转换工具：空闲";
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
            using var dialog = new TrayExitDialog();
            dialog.ShowDialog(this);
            switch (dialog.Choice)
            {
                case TaskCloseChoice.ContinueInTray:
                    HideToTray();
                    break;
                case TaskCloseChoice.CancelTaskAndExit:
                    BeginInvoke(async () => await CancelTaskAndExitAsync());
                    break;
                default:
                    linkInput.Focus();
                    break;
            }

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
        linkInput.Focus();
    }

    private async Task ExitFromTrayAsync()
    {
        if (IsProcessing)
        {
            using var dialog = new TrayExitDialog();
            dialog.ShowDialog();
            if (dialog.Choice == TaskCloseChoice.ContinueInTray)
            {
                UpdateTrayStatus("正在处理");
                return;
            }

            if (dialog.Choice == TaskCloseChoice.ReturnToSoftware)
            {
                ShowMainWindow();
                return;
            }

            await CancelTaskAndExitAsync();
            return;
        }

        exitRequested = true;
        trayIcon.Visible = false;
        Close();
    }

    private void UpdateTrayStatus(string status)
    {
        var text = $"飞船下载转换工具：{status}";
        trayIcon.Text = text.Length <= 63 ? text : text[..63];
    }

    private async Task CancelTaskAndExitAsync()
    {
        stopBatchRequested = true;
        batchCancellation?.Cancel();
        if (batchCompletion is not null)
        {
            await batchCompletion.Task;
        }

        if (workerClient?.IsRunning == true)
        {
            await workerClient.CancelOrTerminateAsync(TimeSpan.FromSeconds(5));
        }

        exitRequested = true;
        trayIcon.Visible = false;
        Close();
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
        IOException when exception.Message.Contains("结果目录", StringComparison.Ordinal) => exception.Message.Trim(),
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

        using var dialog = new SettingsDialog(
            settings.Mp3BitrateKbps,
            credentialsConfigured,
            settings.OutputPreference,
            settings.CustomOutputDirectory);
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
                    settings = settings with
                    {
                        Mp3BitrateKbps = dialog.Mp3BitrateKbps,
                        OutputPreference = dialog.OutputPreference,
                        CustomOutputDirectory = dialog.CustomOutputDirectory
                    };
                    await settingsStore.SaveAsync(settings, CancellationToken.None);
                    ReportStatus(
                        $"设置已保存。最终 MP3 码率为 {settings.Mp3BitrateKbps} kbps；生成文件保存在{DescribeOutputLocation(settings)}。",
                        false);
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
        if (string.IsNullOrWhiteSpace(currentResultDirectory) || !Directory.Exists(currentResultDirectory))
        {
            MessageBox.Show(
                this,
                "当前还没有可打开的任务结果目录。完成一次下载或转换后再按 Alt+O。",
                "结果目录",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information);
            return;
        }

        try
        {
            Process.Start(new ProcessStartInfo
            {
                FileName = currentResultDirectory,
                UseShellExecute = true
            });
        }
        catch (Exception exception) when (
            exception is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            ReportStatus($"无法打开结果目录：{ToActionableError(exception)}", true);
        }
    }

    private void SetCurrentResultDirectory(string? resultPath)
    {
        if (string.IsNullOrWhiteSpace(resultPath))
        {
            return;
        }

        var directory = Directory.Exists(resultPath)
            ? resultPath
            : Path.GetDirectoryName(resultPath);
        if (!string.IsNullOrWhiteSpace(directory))
        {
            currentResultDirectory = directory;
        }
    }

    private void ShowDonation()
    {
        var imagePath = Path.Combine(AppContext.BaseDirectory, "assets", "donation_qr.jpg");
        if (!File.Exists(imagePath))
        {
            var repositoryRoot = FindDevelopmentRepositoryRoot();
            imagePath = repositoryRoot is null
                ? imagePath
                : Path.Combine(repositoryRoot, "worker", "assets", "donation_qr.jpg");
        }

        using var dialog = new DonationDialog(imagePath);
        dialog.ShowDialog(this);
        linkInput.Focus();
    }

    private void ShowHelp()
    {
        using var dialog = new HelpDialog(HelpDialog.LoadContent());
        dialog.ShowDialog(this);
        linkInput.Focus();
    }
}
