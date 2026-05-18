from flask import Flask, render_template, request, send_file
from analyzer import get_ports, generate_excel_bytes

app = Flask(__name__)

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        host = request.form.get('host')
        protocol = request.form.get('protocol')
        username = request.form.get('username')
        password = request.form.get('password')
        secret = request.form.get('secret')
        months = float(request.form.get('months', 6))
        query_type = request.form.get('query_type', 'inactive')

        try:
            # Query the switch
            ports = get_ports(
                host=host,
                username=username,
                password=password,
                secret=secret,
                protocol=protocol,
                months=months,
                query_type=query_type
            )

            if not ports:
                return render_template('index.html', info=f"Connection successful, but no {query_type} ports were found for a {months} month timeframe.")

            # Generate Excel in memory
            excel_bytes = generate_excel_bytes(ports)
            
            # Send file to user
            filename = f"{host}_{query_type}_ports_{months}m.xlsx"
            return send_file(
                excel_bytes,
                as_attachment=True,
                download_name=filename,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )

        except Exception as e:
            return render_template('index.html', error=f"Error analyzing ports: {str(e)}")

    # GET request, render form
    return render_template('index.html')

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)