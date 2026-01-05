import './style.css';
import { initFlowbite } from 'flowbite';

// Initialize Flowbite
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => initFlowbite());
} else {
    initFlowbite();
}

// Also export it to the window object just in case
window.initFlowbite = initFlowbite;

// Have the courage to follow your heart and intuition.
