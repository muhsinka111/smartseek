FROM nginx:latest

# Copy static files to nginx
COPY . /usr/share/nginx/html/

# Expose port 8080 (Railway default)
EXPOSE 8080

# Configure nginx to listen on 8080
RUN sed -i 's/listen 80;/listen 8080;/' /etc/nginx/conf.d/default.conf

CMD ["nginx", "-g", "daemon off;"]

