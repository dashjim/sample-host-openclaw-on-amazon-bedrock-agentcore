import { useState, useEffect, createContext, useContext, type ReactNode } from 'react';
import {
  BrowserRouter,
  Routes,
  Route,
  Navigate,
  useNavigate,
  useLocation,
} from 'react-router-dom';
import { Layout, Menu, Button, Typography, Spin, ConfigProvider, theme as antTheme, Dropdown } from 'antd';
import {
  DashboardOutlined,
  ApiOutlined,
  UserOutlined,
  FolderOutlined,
  LogoutOutlined,
  BulbOutlined,
  BulbFilled,
  DesktopOutlined,
} from '@ant-design/icons';
import { isAuthenticated, signOut, getAdminEmail } from './services/auth';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Channels from './pages/Channels';
import Users from './pages/Users';
import Files from './pages/Files';

const { Header, Sider, Content } = Layout;
const { Text } = Typography;

// --- Theme ---
type ThemeMode = 'light' | 'dark' | 'system';

const ThemeContext = createContext<{
  mode: ThemeMode;
  setMode: (m: ThemeMode) => void;
  isDark: boolean;
}>({ mode: 'system', setMode: () => {}, isDark: false });

export const useTheme = () => useContext(ThemeContext);

function useSystemDark() {
  const [dark, setDark] = useState(
    () => window.matchMedia('(prefers-color-scheme: dark)').matches
  );
  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = (e: MediaQueryListEvent) => setDark(e.matches);
    mq.addEventListener('change', handler);
    return () => mq.removeEventListener('change', handler);
  }, []);
  return dark;
}

function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<ThemeMode>(() => {
    return (localStorage.getItem('oc-admin-theme') as ThemeMode) || 'system';
  });
  const systemDark = useSystemDark();
  const isDark = mode === 'dark' || (mode === 'system' && systemDark);

  useEffect(() => {
    localStorage.setItem('oc-admin-theme', mode);
  }, [mode]);

  return (
    <ThemeContext.Provider value={{ mode, setMode, isDark }}>
      <ConfigProvider
        theme={{
          algorithm: isDark ? antTheme.darkAlgorithm : antTheme.defaultAlgorithm,
        }}
      >
        {children}
      </ConfigProvider>
    </ThemeContext.Provider>
  );
}

// --- Menu ---
const menuItems = [
  { key: '/', icon: <DashboardOutlined />, label: 'Dashboard' },
  { key: '/channels', icon: <ApiOutlined />, label: 'Channels' },
  { key: '/users', icon: <UserOutlined />, label: 'Users' },
  { key: '/files', icon: <FolderOutlined />, label: 'Files' },
];

function ProtectedRoute({ children }: { children: ReactNode }) {
  const [authed, setAuthed] = useState<boolean | null>(null);

  useEffect(() => {
    isAuthenticated().then(setAuthed);
  }, []);

  if (authed === null) {
    return (
      <div style={{ textAlign: 'center', padding: 100 }}>
        <Spin size="large" />
      </div>
    );
  }

  if (!authed) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
}

function ThemeToggle() {
  const { mode, setMode, isDark } = useTheme();

  const items = [
    { key: 'light', icon: <BulbOutlined />, label: 'Light' },
    { key: 'dark', icon: <BulbFilled />, label: 'Dark' },
    { key: 'system', icon: <DesktopOutlined />, label: 'System' },
  ];

  return (
    <Dropdown
      menu={{
        items,
        selectedKeys: [mode],
        onClick: ({ key }) => setMode(key as ThemeMode),
      }}
      trigger={['click']}
    >
      <Button
        type="text"
        icon={isDark ? <BulbFilled /> : <BulbOutlined />}
      />
    </Dropdown>
  );
}

function AppLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState('');
  const [collapsed, setCollapsed] = useState(false);
  const { isDark } = useTheme();

  useEffect(() => {
    getAdminEmail().then(setEmail);
  }, []);

  const handleLogout = async () => {
    await signOut();
    navigate('/login');
  };

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        theme="dark"
        style={{ borderRadius: 0, border: 'none' }}
      >
        <div
          style={{
            height: 32,
            margin: 16,
            color: '#fff',
            fontWeight: 'bold',
            fontSize: collapsed ? 12 : 16,
            textAlign: 'center',
            lineHeight: '32px',
            overflow: 'hidden',
            whiteSpace: 'nowrap',
          }}
        >
          {collapsed ? 'OC' : 'OpenClaw'}
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[location.pathname]}
          items={menuItems}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <Layout>
        <Header
          style={{
            background: isDark ? '#141414' : '#fff',
            padding: '0 24px',
            display: 'flex',
            justifyContent: 'flex-end',
            alignItems: 'center',
            gap: 16,
            borderBottom: `1px solid ${isDark ? '#303030' : '#f0f0f0'}`,
          }}
        >
          <ThemeToggle />
          <Text type="secondary">{email}</Text>
          <Button
            icon={<LogoutOutlined />}
            onClick={handleLogout}
            type="text"
          >
            Logout
          </Button>
        </Header>
        <Content style={{ margin: 24 }}>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/channels" element={<Channels />} />
            <Route path="/users" element={<Users />} />
            <Route path="/files" element={<Files />} />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route
            path="/*"
            element={
              <ProtectedRoute>
                <AppLayout />
              </ProtectedRoute>
            }
          />
        </Routes>
      </BrowserRouter>
    </ThemeProvider>
  );
}
