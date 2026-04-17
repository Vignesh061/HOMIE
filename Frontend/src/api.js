import axios from 'axios';

const API_BASE = 'http://localhost:5000';

const api = axios.create({
    baseURL: API_BASE,
    headers: { 'Content-Type': 'application/json' },
});

// Attach JWT to every request automatically
api.interceptors.request.use((config) => {
    const token = localStorage.getItem('homie_token');
    if (token) {
        config.headers.Authorization = `Bearer ${token}`;
    }
    return config;
});

// ── Auth API ──────────────────────────────────────

export const registerUser = async (name, email, password, role, location, specialization, pricePerHour) => {
    const res = await api.post('/auth/register', {
        name, email, password, role, location, specialization,
        price_per_hour: pricePerHour,
    });
    const { token, user } = res.data;
    localStorage.setItem('homie_token', token);
    return user;
};

export const loginUser = async (email, password) => {
    const res = await api.post('/auth/login', { email, password });
    const { token, user } = res.data;
    localStorage.setItem('homie_token', token);
    return user;
};

export const getMe = async () => {
    const res = await api.get('/auth/me');
    return res.data.user;
};

export const logout = () => {
    localStorage.removeItem('homie_token');
};

export const isLoggedIn = () => {
    return !!localStorage.getItem('homie_token');
};

// ── Professionals API ─────────────────────────────

export const getNearbyProfessionals = async (category = '', search = '', radius = 50) => {
    const params = {};
    if (category && category !== 'all') params.category = category;
    if (search) params.search = search;
    if (radius) params.radius = radius;

    const res = await api.get('/api/professionals/nearby', { params });
    return res.data;
};

export const updateOnlineStatus = async (isOnline) => {
    const res = await api.put('/api/professionals/status', { is_online: isOnline });
    return res.data;
};

export default api;
